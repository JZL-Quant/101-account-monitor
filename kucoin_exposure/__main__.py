import uvicorn

from .app import create_app
from .config import load_config


def main():
    config = load_config()
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
