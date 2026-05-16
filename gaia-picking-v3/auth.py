from functools import wraps
from flask import session, redirect, url_for, request, jsonify


def require_manager(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("role") != "manager":
            if request.is_json:
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.is_json:
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated
