import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .routes import register_routes


def create_app(base_dir, lifespan=None):
    app = FastAPI(lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=os.path.join(base_dir, "static")), name="static")

    templates = Jinja2Templates(directory=os.path.join(base_dir, "templates"))
    register_routes(app, templates)
    return app
