"""Korea AI Telecom mu-law 8 kHz ↔ Gemini signed PCM16. Python 3.12 audioop is intentional."""

import audioop
import base64


class AudioCodec:
    def __init__(self):
        self.input_state = self.output_state = None

    def decode(self, payload):
        raw = base64.b64decode(payload, validate=True)
        if len(raw) > 8000:
            raise ValueError("Audio frame too large")
        pcm = audioop.ulaw2lin(raw, 2)
        pcm, self.input_state = audioop.ratecv(pcm, 2, 1, 8000, 16000, self.input_state)
        return pcm

    def encode(self, pcm):
        pcm, self.output_state = audioop.ratecv(pcm, 2, 1, 24000, 8000, self.output_state)
        return base64.b64encode(audioop.lin2ulaw(pcm, 2)).decode()
