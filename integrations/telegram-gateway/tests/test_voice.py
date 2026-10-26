import asyncio

from telegram_gateway.voice import transcribe_voice


class FakeTelegramFile:
    def __init__(self, payload):
        self.payload = payload
        self.downloaded_to = None

    async def download_to_drive(self, custom_path):
        self.downloaded_to = custom_path
        with open(custom_path, "wb") as handle:
            handle.write(self.payload)


class FakeVoice:
    def __init__(self, payload=b"OggS-fake-opus"):
        self.payload = payload
        self.file = FakeTelegramFile(payload)

    async def get_file(self):
        return self.file


def test_transcribes_and_deletes_the_temp_file(tmp_path):
    voice = FakeVoice()
    seen = {}

    async def transcriber(path):
        assert path.exists(), "the transcriber must receive a real file"
        seen["name"] = path.name
        seen["bytes"] = path.read_bytes()
        return "  hello from the voice note  "

    text = asyncio.run(transcribe_voice(voice, transcriber, tmp_dir=str(tmp_path)))
    assert text == "hello from the voice note"
    assert seen["bytes"] == b"OggS-fake-opus"
    assert seen["name"].endswith(".ogg")
    assert list(tmp_path.iterdir()) == []  # cleaned up


def test_temp_file_is_deleted_even_when_transcription_fails(tmp_path):
    voice = FakeVoice()

    async def failing(path):
        raise RuntimeError("groq said no")

    try:
        asyncio.run(transcribe_voice(voice, failing, tmp_dir=str(tmp_path)))
    except RuntimeError as exc:
        assert "groq said no" in str(exc)
    else:
        raise AssertionError("the failure should propagate")
    assert list(tmp_path.iterdir()) == []
