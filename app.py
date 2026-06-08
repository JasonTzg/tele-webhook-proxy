from flask import Flask

from services.routes import register_routes

app = Flask(__name__)
register_routes(app)


if __name__ == "__main__":
    import os

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=True)
