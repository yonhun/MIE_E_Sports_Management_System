import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

class Config:
    # 개발용이므로 하드코딩해도 괜찮지만, 실제 환경이라면 환경변수로 빼는 것을 권장
    SECRET_KEY = "change_this_secret_key"

    # SQLite 파일 경로
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(BASE_DIR, "app.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 환경변수에서 가져오되, 없으면 빈 문자열
    RIOT_API_KEY = os.environ.get("RIOT_API_KEY", "")
