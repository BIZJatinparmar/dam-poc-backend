"""Backend package setup shared by the API and maintenance commands."""

from pathlib import Path

from dotenv import load_dotenv


# A developer's local file supplies missing settings; deployed environment values win.
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
