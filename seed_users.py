import csv
from werkzeug.security import generate_password_hash

from app import create_app
from models import db, User, Participant, Tournament
from riot_api import get_summoner_ranks


CSV_FILE = "users.csv"


def seed_users():
    app = create_app()

    with app.app_context():
        # 토너먼트가 없으면 하나 생성
        tournament = Tournament.query.first()
        if tournament is None:
            tournament = Tournament(name="Default Tournament")
            db.session.add(tournament)
            db.session.commit()

        # UTF-8 BOM 있는 경우도 처리 가능하게 utf-8-sig 사용
        with open(CSV_FILE, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)

            for row in reader:
                username = (row.get("username") or "").strip()
                password = (row.get("password") or "").strip()
                student_id = (row.get("student_id") or "").strip()
                real_name = (row.get("real_name") or "").strip()
                riot_id = (row.get("riot_id") or "").strip()
                # 🚨 수정: 다중 역할을 대비하여 role을 쉼표 기준으로 처리하도록 로직을 변경합니다.
                # 그러나 CSV는 단일 역할만 제공한다고 가정하고 일단 role 필드만 사용합니다.
                role_input = (row.get("role") or "").strip().upper()
                role = role_input if role_input in ("USER", "ADMIN") else "USER"
                
                actual_score_str = (row.get("actual_score") or "").strip()
                primary_role = (row.get("primary_role") or "").strip()
                secondary_role1 = (row.get("secondary_role1") or "").strip()
                secondary_role2 = (row.get("secondary_role2") or "").strip()

                if not username or not password:
                    print(f"Skipping row without username/password: {row}")
                    continue

                # 이미 존재하는 username 이면 스킵
                existing = User.query.filter_by(username=username).first()
                if existing:
                    print(f"User '{username}' already exists. Skipping.")
                    continue

                user = User(
                    username=username,
                    password_hash=generate_password_hash(password),
                    role=role,
                    # 🚨 추가: 모든 시드된 사용자는 APPROVED 상태로 설정
                    approval_status="APPROVED",
                    student_id=student_id or None,
                    real_name=real_name or None,
                    summoner_riot_id=riot_id or None,
                    primary_role=primary_role or None,
                    secondary_role1=secondary_role1 or None,
                    secondary_role2=secondary_role2 or None,
                )

                # Riot ID 있으면 티어/puuid 조회
                if riot_id:
                    solo_tier, flex_tier, puuid = get_summoner_ranks(riot_id)
                    user.solo_tier = solo_tier
                    user.flex_tier = flex_tier
                    user.puuid = puuid

                    # 단일 tier 필드는 솔로 우선, 없으면 플렉스
                    if solo_tier:
                        user.tier = solo_tier
                    elif flex_tier:
                        user.tier = flex_tier

                # actual_score 값 있으면 저장
                if actual_score_str:
                    try:
                        user.actual_score = int(actual_score_str)
                    except ValueError:
                        print(f"Invalid actual_score '{actual_score_str}' for user '{username}'")

                db.session.add(user)
                db.session.flush()  # user.id 확보

                # USER 계정은 자동 참가 신청(PENDING), ADMIN은 신청 안 함
                # (USER Role만 확인하므로, CSV에서 USER,ADMIN 으로 설정된 경우 USER로 간주하고 신청합니다.)
                if "USER" in user.role: 
                    existing_participant = Participant.query.filter_by(
                        user_id=user.id,
                        tournament_id=tournament.id,
                    ).first()

                    if not existing_participant:
                        participant = Participant(
                            user_id=user.id,
                            tournament_id=tournament.id,
                            status="PENDING",
                        )
                        db.session.add(participant)
                        print(f"User '{username}' auto-applied to tournament '{tournament.name}'.")

            db.session.commit()
            print("User seeding (with auto apply) completed.")


if __name__ == "__main__":
    seed_users()