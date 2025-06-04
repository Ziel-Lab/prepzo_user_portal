from flask import Blueprint, jsonify

main_bp = Blueprint("main", __name__)

@main_bp.route("/health")
def health():
    return jsonify({"status": "ok", "app": "prepzo-user-portal"})

@main_bp.route("/")
def home():
    return jsonify({"message": "Hello, Prepzo-user-portal!"})
