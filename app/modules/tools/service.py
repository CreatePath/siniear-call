from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.modules.calls.transcripts import save_turns


class ToolSession:
    """Identifiers and confirmation evidence are bound by the server session, never the model."""

    def __init__(self, engine, call_id, client, memory_search=None):
        self.engine, self.call_id, self.client = engine, call_id, client
        self.memory_search = memory_search
        self.meetings, self.registrations = {}, {}
        self.pending = None
        self.announcement_turn_id = self.confirmation_turn_id = None
        self.announcement_played = False
        self.last_offset = -1
        self.request_in_flight = False

    def played(self, turn_id):
        if turn_id == self.announcement_turn_id:
            self.announcement_played = True

    def interrupt(self):
        if self.request_in_flight:
            return
        self.announcement_turn_id = self.confirmation_turn_id = None
        self.announcement_played = False

    def record(self, speaker, text, offset_ms, allow_confirmation=True):
        if not text.strip():
            return
        turn_id = str(uuid4())
        offset_ms = max(self.last_offset + 1, offset_ms)
        self.last_offset = offset_ms
        context = None
        if self.pending and not self.request_in_flight:
            if (
                speaker == "assistant"
                and allow_confirmation
                and self.pending["announcement"] in text
            ):
                context = self.pending["context"]
                self.announcement_turn_id = turn_id
                self.confirmation_turn_id = None
                self.announcement_played = False
            elif (
                speaker == "user"
                and allow_confirmation
                and self.announcement_turn_id
                and self.announcement_played
            ):
                self.confirmation_turn_id = None
                if text.strip().rstrip(".!。") in {
                    "네",
                    "예",
                    "동의합니다",
                    "신청해 주세요",
                    "취소해 주세요",
                }:
                    context = {
                        **self.pending["context"],
                        "announcement_turn_id": self.announcement_turn_id,
                    }
                    self.confirmation_turn_id = turn_id
                else:
                    self.interrupt()
            elif speaker == "assistant":
                self.interrupt()
        with Session(self.engine) as session:
            save_turns(
                session,
                self.call_id,
                [
                    {
                        "turn_id": turn_id,
                        "speaker": speaker,
                        "text": text,
                        "offset_ms": offset_ms,
                        "action_context": context,
                    }
                ],
            )
        return turn_id

    def execute(self, name, args):
        allowed = {
            "search_meetings": set(),
            "list_registrations": set(),
            "begin_registration": {"meeting_id"},
            "begin_cancellation": {"registration_id"},
            "confirm_pending": set(),
            "search_memories": {"query"},
        }
        if (
            name not in allowed
            or not isinstance(args, dict)
            or set(args) != allowed[name]
            or any(not isinstance(value, str) for value in args.values())
        ):
            return {"error": "INVALID_TOOL_ARGUMENTS"}
        prefix = f"/internal/calls/{self.call_id}"
        if name == "search_memories":
            return self.memory_search(args["query"]) if self.memory_search else {"items": []}
        if name in {"search_meetings", "list_registrations"}:
            path = (
                "/meeting-search"
                if name == "search_meetings"
                else "/registrations?status=confirmed"
            )
            method = "POST" if name == "search_meetings" else "GET"
            status, result = self.client.request(
                method, prefix + path, {} if method == "POST" else None
            )
            if status != 200:
                return {"error": "SERVICE_UNAVAILABLE"}
            target = self.meetings if name == "search_meetings" else self.registrations
            target.clear()
            target.update({str(item["id"]): item for item in result["items"]})
            return result
        if name.startswith("begin_"):
            if self.request_in_flight:
                return {
                    "error": "RESULT_UNKNOWN",
                    "instruction": "기존 작업의 결과를 먼저 확인하세요.",
                }
            registration = None
            if name == "begin_registration":
                meeting = self.meetings.get(args["meeting_id"])
                action = "register"
            else:
                registration = self.registrations.get(args["registration_id"])
                meeting = registration["meeting"] if registration else None
                action = "cancel"
            if not meeting:
                return {"error": "LOOK_UP_TARGET_FIRST"}
            word = "참가 신청" if action == "register" else "참가 신청 취소"
            starts = datetime.fromisoformat(meeting["starts_at"]).astimezone(ZoneInfo("Asia/Seoul"))
            weekday = "월화수목금토일"[starts.weekday()]
            period = "오전" if starts.hour < 12 else "오후"
            minute = f" {starts.minute}분" if starts.minute else ""
            when = (
                f"{starts.year}년 {starts.month}월 {starts.day}일 {weekday}요일 "
                f"{period} {starts.hour % 12 or 12}시{minute}"
            )
            announcement = f"{meeting['title']}, {when} 일정에 {word}할까요?"
            context = {"action": action, "meeting_id": str(meeting["id"])}
            if registration:
                context.update(
                    registration_id=str(registration["id"]),
                    expected_version=registration["version"],
                )
            self.pending = {"context": context, "announcement": announcement}
            self.announcement_turn_id = self.confirmation_turn_id = None
            self.announcement_played = False
            return {
                "read_exactly": announcement,
                "instruction": "이 문장을 그대로 안내하고 사용자의 명시적 응답을 기다리세요.",
            }
        if not self.pending or not self.confirmation_turn_id:
            return {
                "error": "CONFIRMATION_REQUIRED",
                "instruction": "대상과 작업을 다시 안내하고 명시적 동의를 기다리세요.",
            }
        context = self.pending["context"]
        payload = {"confirmation_turn_id": self.confirmation_turn_id}
        if context["action"] == "register":
            path = "/registrations"
            payload["meeting_id"] = context["meeting_id"]
        else:
            path = f"/registrations/{context['registration_id']}/cancel"
            payload["expected_version"] = context["expected_version"]
        key = f"tool:{self.call_id}:{self.confirmation_turn_id}"
        # Retry an ambiguous timeout only with this exact target, evidence and key.
        try:
            self.request_in_flight = True
            status, result = self.client.request("POST", prefix + path, payload, key)
        except (TimeoutError, OSError):
            return {
                "error": "RESULT_UNKNOWN",
                "instruction": "완료를 안내하지 말고 같은 작업의 결과를 다시 확인하세요.",
            }
        if status >= 500 or status == 429:
            return {"error": "RESULT_UNKNOWN", "instruction": "동일한 작업으로 결과를 확인하세요."}
        self.request_in_flight = False
        if status not in {200, 201}:
            self.confirmation_turn_id = None
            return {"error": "ACTION_REJECTED", "http_status": status}
        self.pending = None
        self.confirmation_turn_id = None
        return {"result": result, "confirmed_by_service": True}


def declarations():
    specs = [
        ("search_meetings", "본인 지역의 참여 가능한 모임 검색", None),
        ("list_registrations", "본인의 현재 참가 신청 목록", None),
        ("begin_registration", "검색된 일정을 안내하고 신청 동의 질문 준비", "meeting_id"),
        ("begin_cancellation", "조회된 신청을 안내하고 취소 동의 질문 준비", "registration_id"),
        (
            "confirm_pending",
            "저장된 명시적 동의로 신청 또는 취소 실행. 성공 응답 전 완료 안내 금지",
            None,
        ),
        ("search_memories", "본인의 허용된 과거 통화 검색", "query"),
    ]
    return [
        {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {field: {"type": "string"}} if field else {},
                "required": [field] if field else [],
            },
        }
        for name, description, field in specs
    ]
