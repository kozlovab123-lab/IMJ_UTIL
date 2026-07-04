import argparse

from imj_util.config import settings
from imj_util.web_viewer import run_viewer


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only web viewer for IMJ_UTIL analysis database",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    args = parser.parse_args()

    if not settings.database_path.exists():
        print(f"База пока не создана: {settings.database_path}")
        print("Запустите анализ или дождитесь первых записей.")

    try:
        run_viewer(host=args.host, port=args.port)
    except KeyboardInterrupt:
        print("\nViewer stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
