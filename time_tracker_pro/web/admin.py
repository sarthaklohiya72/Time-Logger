from __future__ import annotations

from typing import Optional

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from ..core.rows import display_name, row_value
from ..repositories.admin import delete_user_cascade, list_users
from ..repositories.users import get_user_by_id
from ..services.account_deletion_email import send_account_deletion_email
from .decorators import admin_required
from .utils import get_current_user_id


bp = Blueprint("admin", __name__)


@bp.route("/admin/users", endpoint="admin_users")
@admin_required
def admin_users():
    db_name = current_app.config["DB_NAME"]
    admin_row = get_user_by_id(db_name, int(get_current_user_id() or 0))
    current_user = {
        "id": int(admin_row["id"]) if admin_row else 0,
        "display_name": display_name(admin_row),
        "role": (row_value(admin_row, "role") if admin_row else "admin"),
    }
    users = list_users(db_name)
    return render_template(
        "admin_users.html",
        users=users,
        current_user=current_user,
        error=request.args.get("error"),
        success=request.args.get("success"),
    )


@bp.route("/admin/delete-user", methods=["POST"], endpoint="admin_delete_user")
@admin_required
def admin_delete_user():
    db_name = current_app.config["DB_NAME"]
    target_id = (request.form.get("user_id") or "").strip()
    message = (request.form.get("message") or "").strip()
    admin_id = int(get_current_user_id() or 0)

    if not target_id:
        return redirect(url_for("admin.admin_users", error="Missing user selection."))
    try:
        target_id_int = int(target_id)
    except Exception:
        return redirect(url_for("admin.admin_users", error="Invalid user id."))

    if target_id_int == admin_id:
        return redirect(url_for("admin.admin_users", error="You cannot delete your own account."))

    if not message:
        return redirect(url_for("admin.admin_users", error="Please include a message to the user."))

    user = get_user_by_id(db_name, target_id_int)
    if not user:
        return redirect(url_for("admin.admin_users", error="User not found."))
    if (row_value(user, "role") or "user") == "admin":
        return redirect(url_for("admin.admin_users", error="You cannot delete another admin."))

    delete_user_cascade(db_name, target_id_int)

    email_sent = send_account_deletion_email(user, message)
    if not email_sent:
        return redirect(url_for("admin.admin_users", success="User deleted. Email failed to send."))

    return redirect(url_for("admin.admin_users", success="User deleted and notified."))
