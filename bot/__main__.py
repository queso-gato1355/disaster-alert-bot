import logging
import sys

from . import config
from .client import AlertBot


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    cfg = config.load()
    if not cfg.discord_token:
        sys.exit("DISCORD_TOKEN 이 설정되지 않았습니다 (.env 확인)")
    AlertBot(cfg).run(cfg.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
