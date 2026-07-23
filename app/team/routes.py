import hashlib
import secrets
from datetime import datetime, timedelta

from email_validator import EmailNotValidError, validate_email
from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_babel import gettext
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app import db
from app.models import OrganizationInvitation, OrganizationMember, User
from app.team.services import active_membership, require_permission


team = Blueprint("team", __name__, url_prefix="/team")
INVITATION_LIFETIME = timedelta(hours=48)


def _token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _managed_member(member_id, actor_membership):
    member = db.session.get(OrganizationMember, member_id)
    if (
        not member
        or member.organization_id != actor_membership.organization_id
        or member.role == "OWNER"
    ):
        return None
    return member


def _send_invitation_email(invitation, raw_token, existing_user=None):
    from app.routes import _public_url, send_email

    invitation_url = _public_url(url_for("team.accept", token=raw_token))
    language = (
        getattr(existing_user, "preferred_language", None)
        or getattr(
            getattr(invitation.invited_by_member, "user", None),
            "preferred_language",
            None,
        )
        or "es"
    )
    from flask_babel import force_locale

    with force_locale(language):
        subject = gettext("Te invitaron a trabajar en PATIA")
        html = render_template(
            "emails/team_invitation.html",
            organization=invitation.organization,
            invitation_url=invitation_url,
            role=invitation.role,
        )
    return send_email(
        to=invitation.email,
        subject=subject,
        html=html,
        language=language,
    )


@team.get("")
@require_permission("manage_employees")
def index():
    from app.routes import current_user

    membership = active_membership(current_user())
    members = (
        OrganizationMember.query.options(selectinload(OrganizationMember.user)).filter_by(
            organization_id=membership.organization_id
        )
        .order_by(OrganizationMember.is_active.desc(), OrganizationMember.created_at)
        .all()
    )
    invitations = (
        OrganizationInvitation.query.filter_by(
            organization_id=membership.organization_id,
            accepted_at=None,
        )
        .order_by(OrganizationInvitation.created_at.desc())
        .all()
    )
    return render_template(
        "team.html",
        organization=membership.organization,
        members=members,
        invitations=invitations,
        now=datetime.utcnow(),
    )


@team.post("/invite")
@require_permission("manage_employees")
def invite():
    from app.routes import current_user

    actor = active_membership(current_user())
    email = request.form.get("email", "").strip().lower()
    role = request.form.get("role", "CASHIER").strip().upper()
    try:
        validate_email(email, check_deliverability=False)
    except EmailNotValidError:
        flash(gettext("Escribe un correo válido."), "danger")
        return redirect(url_for("team.index"))
    if role not in {"MANAGER", "CASHIER"}:
        flash(gettext("Selecciona un tipo de acceso válido."), "danger")
        return redirect(url_for("team.index"))

    existing_user = User.query.filter_by(email=email).first()
    if existing_user and OrganizationMember.query.filter_by(
        organization_id=actor.organization_id,
        user_id=existing_user.id,
    ).first():
        flash(gettext("Esa persona ya pertenece al equipo."), "warning")
        return redirect(url_for("team.index"))
    if existing_user and OrganizationMember.query.filter_by(
        user_id=existing_user.id
    ).first():
        flash(
            gettext(
                "Ese correo ya pertenece a otra empresa en PATIA. Usa un correo distinto para esta persona."
            ),
            "warning",
        )
        return redirect(url_for("team.index"))

    token = secrets.token_urlsafe(32)
    invitation = OrganizationInvitation.query.filter_by(
        organization_id=actor.organization_id,
        email=email,
    ).first()
    if invitation is None:
        invitation = OrganizationInvitation(
            organization_id=actor.organization_id,
            email=email,
        )
    invitation.invited_by_member_id = actor.id
    invitation.role = role
    invitation.token_hash = _token_hash(token)
    invitation.expires_at = datetime.utcnow() + INVITATION_LIFETIME
    invitation.accepted_at = None
    db.session.add(invitation)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash(gettext("No pudimos crear la invitación."), "danger")
        return redirect(url_for("team.index"))

    sent = _send_invitation_email(invitation, token, existing_user)
    if sent:
        flash(gettext("Invitación enviada."), "success")
    else:
        flash(
            gettext("La invitación quedó creada, pero el correo no pudo enviarse. Intenta reenviarlo."),
            "warning",
        )
    return redirect(url_for("team.index"))


@team.post("/invitations/<int:invitation_id>/resend")
@require_permission("manage_employees")
def resend_invitation(invitation_id):
    from app.routes import current_user

    actor = active_membership(current_user())
    invitation = OrganizationInvitation.query.filter_by(
        id=invitation_id,
        organization_id=actor.organization_id,
        accepted_at=None,
    ).first_or_404()
    token = secrets.token_urlsafe(32)
    invitation.token_hash = _token_hash(token)
    invitation.expires_at = datetime.utcnow() + INVITATION_LIFETIME
    invitation.invited_by_member_id = actor.id
    db.session.commit()
    existing_user = User.query.filter_by(email=invitation.email).first()
    if _send_invitation_email(invitation, token, existing_user):
        flash(gettext("Invitación reenviada."), "success")
    else:
        flash(gettext("No pudimos enviar el correo. Intenta nuevamente."), "warning")
    return redirect(url_for("team.index"))


@team.post("/invitations/<int:invitation_id>/revoke")
@require_permission("manage_employees")
def revoke_invitation(invitation_id):
    from app.routes import current_user

    actor = active_membership(current_user())
    invitation = OrganizationInvitation.query.filter_by(
        id=invitation_id,
        organization_id=actor.organization_id,
        accepted_at=None,
    ).first_or_404()
    db.session.delete(invitation)
    db.session.commit()
    flash(gettext("Invitación cancelada."), "success")
    return redirect(url_for("team.index"))


@team.route("/accept/<token>", methods=["GET", "POST"])
def accept(token):
    from app.routes import current_user

    invitation_query = OrganizationInvitation.query.filter_by(
        token_hash=_token_hash(token), accepted_at=None
    )
    invitation = (
        invitation_query.with_for_update().first()
        if request.method == "POST"
        else invitation_query.first()
    )
    if not invitation or invitation.expires_at <= datetime.utcnow():
        return render_template("team_accept.html", invitation=None), 410

    existing_user = User.query.filter_by(email=invitation.email).first()
    authenticated_user = current_user() if session.get("user_id") else None
    if existing_user and (
        authenticated_user is None or authenticated_user.id != existing_user.id
    ):
        session["pending_invitation_token"] = token
        flash(gettext("Inicia sesión con el correo que recibió la invitación."), "info")
        return redirect(url_for("main.login", next=url_for("team.accept", token=token)))

    if request.method == "POST":
        user = existing_user
        if user is None:
            first_name = request.form.get("first_name", "").strip()
            last_name = request.form.get("last_name", "").strip()
            password = request.form.get("password", "")
            if not first_name or not last_name or len(password) < 8:
                flash(
                    gettext("Completa tu nombre y usa una contraseña de al menos 8 caracteres."),
                    "danger",
                )
                return render_template("team_accept.html", invitation=invitation)
            user = User(
                email=invitation.email,
                company_name=invitation.organization.name,
                first_name=first_name,
                last_name=last_name,
                email_verified=True,
                preferred_language=session.get("language", "es"),
                timezone=invitation.organization.timezone,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.flush()

        other_membership = OrganizationMember.query.filter(
            OrganizationMember.user_id == user.id,
            OrganizationMember.organization_id != invitation.organization_id,
        ).first()
        if other_membership:
            db.session.rollback()
            flash(
                gettext(
                    "Ese correo ya tiene acceso a otra empresa. Solicita una invitación para un correo diferente."
                ),
                "danger",
            )
            return render_template("team_accept.html", invitation=invitation), 409

        member = OrganizationMember.query.filter_by(
            organization_id=invitation.organization_id,
            user_id=user.id,
        ).first()
        if member is None:
            member = OrganizationMember(
                organization_id=invitation.organization_id,
                user_id=user.id,
            )
        member.role = invitation.role
        member.is_active = True
        invitation.accepted_at = datetime.utcnow()
        login_token = secrets.token_hex(32)
        user.session_token = login_token
        db.session.add(member)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            current_app.logger.warning(
                "No se pudo aceptar una invitación de equipo por conflicto de membresía."
            )
            flash(gettext("La invitación ya no pudo aplicarse. Solicita una nueva."), "danger")
            return render_template("team_accept.html", invitation=None), 409

        session.clear()
        session["user_id"] = user.id
        session["language"] = user.preferred_language
        session["organization_id"] = invitation.organization_id
        session["session_token"] = login_token
        flash(gettext("Tu acceso al equipo está listo."), "success")
        return redirect(url_for("main.dashboard"))

    return render_template("team_accept.html", invitation=invitation)


@team.post("/members/<int:member_id>/role")
@require_permission("manage_employees")
def change_role(member_id):
    from app.routes import current_user

    actor = active_membership(current_user())
    member = _managed_member(member_id, actor)
    role = request.form.get("role", "").upper()
    if not member or role not in {"MANAGER", "CASHIER"}:
        flash(gettext("No se pudo cambiar ese tipo de acceso."), "danger")
        return redirect(url_for("team.index"))
    member.role = role
    db.session.commit()
    flash(gettext("Tipo de acceso actualizado."), "success")
    return redirect(url_for("team.index"))


@team.post("/members/<int:member_id>/toggle")
@require_permission("manage_employees")
def toggle_member(member_id):
    from app.routes import current_user

    actor = active_membership(current_user())
    member = _managed_member(member_id, actor)
    if not member:
        flash(gettext("No se pudo actualizar a esa persona."), "danger")
        return redirect(url_for("team.index"))
    member.is_active = not member.is_active
    db.session.commit()
    flash(gettext("Acceso de la persona actualizado."), "success")
    return redirect(url_for("team.index"))


@team.post("/members/<int:member_id>/pin")
@require_permission("manage_employees")
def reset_pin(member_id):
    from app.routes import current_user

    actor = active_membership(current_user())
    member = _managed_member(member_id, actor)
    pin = request.form.get("pin", "").strip()
    if not member or (pin and (not pin.isdigit() or not 4 <= len(pin) <= 6)):
        flash(gettext("El PIN debe tener entre 4 y 6 dígitos."), "danger")
        return redirect(url_for("team.index"))
    member.pin_hash = None
    if pin:
        member.set_pin(pin)
    db.session.commit()
    flash(gettext("PIN actualizado."), "success")
    return redirect(url_for("team.index"))
