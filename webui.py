import asyncio
import os

import hypercorn.asyncio
from hypercorn.config import Config
from quart import (
    Quart,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)

from .backend.category_manager import CategoryManager
from .backend.api import api
from .config import MEMES_DIR
from .image_host.img_sync import ImageSync


def _build_plugin_config(config=None):
    config = config or {}
    img_sync = config.get("img_sync")
    if img_sync is None and config.get("img_sync_config"):
        img_sync = ImageSync(
            config=config["img_sync_config"],
            local_dir=MEMES_DIR,
            provider_type=config.get("img_sync_provider_type", "stardots"),
        )

    return {
        "img_sync": img_sync,
        "category_manager": config.get("category_manager") or CategoryManager(),
        "webui_port": config.get("webui_port", 5000),
    }


class ServerState:
    _instance = None

    def __new__(cls):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance.ready = asyncio.Event()
            cls._instance.port = 5000
        return cls._instance


app = Quart(__name__)

# 注册API蓝图
app.register_blueprint(api, url_prefix="/api")

SERVER_LOGIN_KEY = None
_current_server = None


@app.route("/health", methods=["GET"])
async def health_check():
    """健康检查接口"""
    return jsonify({"status": "running", "version": "1.0"})


@app.before_request
async def require_login():
    allowed_endpoints = ["login", "static"]
    if request.endpoint not in allowed_endpoints and not session.get("authenticated"):
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
async def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        form_data = await request.form
        key = form_data.get("key")
        if key == SERVER_LOGIN_KEY:
            session["authenticated"] = True
            return redirect(url_for("index"))
        else:
            error = "秘钥错误，请重试。"
    return await render_template("login.html", error=error)


@app.route("/")
async def index():
    if not session.get("authenticated"):
        return redirect(url_for("login"))
    return await render_template("index.html")


@app.route("/memes/<category>/<filename>")
async def serve_emoji(category, filename):
    category_path = os.path.join(MEMES_DIR, category)
    if os.path.exists(os.path.join(category_path, filename)):
        return await send_from_directory(category_path, filename)
    else:
        return "File not found: " + os.path.join(category_path, filename), 404


# 提供同步的入口
def run_server(config):
    asyncio.run(start_server(config))


async def start_server(config=None):
    """启动服务器"""
    global SERVER_LOGIN_KEY, _current_server

    state = ServerState()
    state.ready.clear()

    port = config.get("webui_port", 5000)
    SERVER_LOGIN_KEY = config.get("server_key")

    # 配置应用
    app.secret_key = os.urandom(16)
    app.config["PLUGIN_CONFIG"] = _build_plugin_config(config)

    @app.before_serving
    async def notify_ready():
        state.ready.set()

    # 启动服务器
    hypercorn_config = Config()
    hypercorn_config.bind = [f"0.0.0.0:{port}"]
    hypercorn_config.graceful_timeout = 5

    _current_server = await hypercorn.asyncio.serve(
        app,
        hypercorn_config,
    )
    return SERVER_LOGIN_KEY


async def create_app(config=None):
    app = Quart(__name__)

    if config is not None and hasattr(config, "get"):
        app.config["PLUGIN_CONFIG"] = _build_plugin_config(config)
    else:
        print("警告: 配置格式不正确")

    # 注册蓝图
    app.register_blueprint(api, url_prefix="/api")

    return app
