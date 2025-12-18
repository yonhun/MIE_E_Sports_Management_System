from functools import wraps
from flask import session, redirect, url_for, flash
from models import db, User

def current_user():
    """세션에 저장된 user_id를 기반으로 현재 사용자 객체를 반환하는 함수"""
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)

def login_required(role=None):
    """
    로그인이 필요한 라우트를 보호하는 데코레이터.
    특정 role이 지정된 경우 권한 체크도 수행함.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            active_role = session.get("role") 
            
            # 비로그인 상태 체크
            if not user or not active_role:
                return redirect(url_for("auth.login_page"))
            
            # 권한 체크
            if role and active_role != role:
                flash("권한이 거부되었습니다. 현재 권한은 " + active_role + "입니다.")
                return redirect(url_for("auth.login_page"))
            
            return fn(*args, **kwargs)
        return wrapper
    return decorator