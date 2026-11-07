import os

import uvicorn

from . import config
from .app import create_app
from .config import HOST
from .tts import FakeSynth, RoutingSynth


def main() -> None:
    synth = FakeSynth() if os.environ.get("KOKORO_READER_FAKE_TTS") == "1" else RoutingSynth()
    app = create_app(config.home(), synth)
    uvicorn.run(app, host=HOST, port=config.port(), log_level="info", access_log=False)


if __name__ == "__main__":
    main()
