from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User

bp = Blueprint('auth', __name__)

@bp.route("/", methods=["GET"])
def login_page():
    """로그인 페이지 렌더링"""
    return render_template("login.html", roles=None)


@bp.route("/login", methods=["POST"])
def login():
    """로그인 로직 처리 (1단계: 인증, 2단계: 권한 선택)"""
    username = request.form.get("username")
    password = request.form.get("password")
    selected_role = request.form.get("selected_role")

    user = User.query.filter_by(username=username).first()

    if selected_role:
        # 권한 선택 단계
        if not user:
            flash("권한 선택을 위한 사용자를 찾을 수 없습니다.")
            return redirect(url_for("auth.login_page"))
        
        # 승인 상태 확인
        if user.approval_status != "APPROVED":
            if user.approval_status == "REJECTED":
                db.session.delete(user)
                db.session.commit()
                flash(f"로그인에 실패했습니다. 요청이 거부되어 삭제되었습니다.")
                return redirect(url_for("auth.login_page"))
                
            flash(f"로그인에 실패했습니다. 상태: '{user.approval_status}'.")
            return redirect(url_for("auth.login_page"))
            
        all_roles = [r.strip() for r in user.role.split(',') if r.strip()]
        if selected_role not in all_roles:
            flash("유효하지 않은 권한 선택입니다.")
            return redirect(url_for("auth.login_page"))
        active_role = selected_role
        
    else:
        # 1차 인증 단계
        if not user or not check_password_hash(user.password_hash, password):
            flash("유효하지 않은 아이디 또는 비밀번호입니다.")
            return redirect(url_for("auth.login_page"))

        # 승인 상태 확인
        if user.approval_status != "APPROVED":
            if user.approval_status == "REJECTED":
                db.session.delete(user)
                db.session.commit()
                flash(f"로그인에 실패했습니다. 요청이 거부되어 삭제되었습니다.")
                return redirect(url_for("auth.login_page"))
            flash(f"로그인에 실패했습니다. 상태: '{user.approval_status}'.")
            return redirect(url_for("auth.login_page"))
        
        # 다중 권한 처리
        all_roles = [r.strip() for r in user.role.split(',') if r.strip()]
        if len(all_roles) > 1:
            return render_template("login.html", username=username, roles=all_roles)
        else:
            active_role = all_roles[0] if all_roles else "USER"
            

    # 세션 설정 및 리다이렉트
    if 'active_role' in locals():
        session["user_id"] = user.id
        session["role"] = active_role
        if active_role == "ADMIN":
            return redirect(url_for("admin.admin_dashboard"))
        else:
            return redirect(url_for("user.user_dashboard"))
    
    return redirect(url_for("auth.login_page"))


@bp.route("/register", methods=["GET", "POST"])
def register():
    """회원가입 처리"""
    if request.method == "GET":
        return render_template("register.html")

    username = request.form.get("username")
    password = request.form.get("password")
    initial_role_selection = request.form.get("initial_role", "USER").upper()

    if not username or not password:
        flash("아이디와 비밀번호는 필수 입력 사항입니다.")
        return redirect(url_for("auth.register"))

    if User.query.filter_by(username=username).first():
        flash("이미 존재하는 아이디입니다.")
        return redirect(url_for("auth.register"))

    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        role=initial_role_selection, 
        approval_status="PENDING", 
    )
    db.session.add(user)
    db.session.commit()

    flash("회원가입이 완료되었습니다. 관리자 승인을 기다려주세요.")
    return redirect(url_for("auth.login_page"))


@bp.route("/logout")
def logout():
    """로그아웃 및 세션 초기화"""
    session.clear()
    return redirect(url_for("auth.login_page"))