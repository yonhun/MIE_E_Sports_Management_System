from flask import Flask
from werkzeug.security import generate_password_hash

from config import Config
from models import db, User
from services import (
    calculate_estimated_score, get_user_score, get_member_weighted_score
)

# 블루프린트 임포트
from routes.auth import bp as auth_bp
from routes.user import bp as user_bp
from routes.admin import bp as admin_bp
from routes.team import bp as team_bp
from routes.tournament import bp as tournament_bp

def create_app():
    """애플리케이션 팩토리 함수"""
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    # 블루프린트 등록
    app.register_blueprint(auth_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(team_bp)
    app.register_blueprint(tournament_bp)

    # 컨텍스트 프로세서 (템플릿 전역 헬퍼 함수)
    @app.context_processor
    def inject_helpers():
        return dict(
            calculate_estimated_score=calculate_estimated_score,
            get_user_score=get_user_score,
            get_member_weighted_score=get_member_weighted_score,
            max=max,
        )

    # DB 및 초기 관리자 계정 설정
    with app.app_context():
        db.create_all()
        if db.session.get(User, 1) is None:
            admin_user = User(
                username="admin",
                password_hash=generate_password_hash("admin"),
                role="ADMIN",
                approval_status="APPROVED", 
            )
            db.session.add(admin_user)
            db.session.commit()
            
    return app

app = create_app()

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)