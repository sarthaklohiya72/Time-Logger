from __future__ import annotations

import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from flask import Flask, url_for

from .core.time_utils import human_hours
from .db import init_db
from .repositories.app_settings import get_app_setting


def create_app(config_overrides: Optional[Dict[str, Any]] = None) -> Flask:
    load_dotenv()

    root = Path(__file__).resolve().parents[1]
    template_folder = str(root / "templates")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    app = Flask(__name__, template_folder=template_folder)

    secret = os.getenv("SECRET_KEY")
    if not secret or secret == "dev-secret-change-me":
        env = (os.getenv("FLASK_ENV") or os.getenv("ENV") or "").strip().lower()
        allow_insecure = env in {"dev", "development"} or os.getenv("ALLOW_INSECURE_SECRET") == "1"
        if not allow_insecure:
            raise RuntimeError(
                "SECRET_KEY is missing or insecure. Set a strong random SECRET_KEY in the environment before running in production."
            )
        secret = secret or "dev-secret-change-me"
    app.secret_key = secret
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if os.getenv("SESSION_COOKIE_SECURE"):
        app.config["SESSION_COOKIE_SECURE"] = True
    app.permanent_session_lifetime = timedelta(days=int(os.getenv("SESSION_LIFETIME_DAYS", "30")))

    app.config["DB_NAME"] = (os.getenv("DB_PATH") or "productivity.db").strip() or "productivity.db"

    if config_overrides:
        app.config.update(config_overrides)

    init_db(app.config["DB_NAME"])

    app.jinja_env.filters["human_hours"] = human_hours

    from .web.admin import bp as admin_bp
    from .web.api import bp as api_bp
    from .web.auth import bp as auth_bp
    from .web.main import bp as main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(main_bp)

    def _icon_version() -> str:
        try:
            return get_app_setting(app.config["DB_NAME"], "app_icon_version") or "1"
        except Exception:
            return "1"

    @app.context_processor
    def inject_pwa_assets():
        version = _icon_version()
        return {
            "pwa_manifest_url": url_for("main.manifest"),
            "pwa_icon_url": url_for("main.app_icon", v=version),
            "pwa_icon_version": version,
        }

    return app
