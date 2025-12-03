from datetime import datetime
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    role = db.Column(db.String(50), default="USER")  # USER, ADMIN
    approval_status = db.Column(db.String(16), default="PENDING") # PENDING, APPROVED, REJECTED

    # 추가 필드
    student_id = db.Column(db.String(20))      # 학번
    real_name = db.Column(db.String(64))       # 이름

    # Riot 관련
    summoner_riot_id = db.Column(db.String(64))  # "닉네임#태그" 형식 (예: Clyde00#KR1)
    puuid = db.Column(db.String(128))

    # 솔로/자유 랭크 티어
    tier = db.Column(db.String(32))
    solo_tier = db.Column(db.String(32))  # 예: "PLATINUM IV"
    flex_tier = db.Column(db.String(32))  # 예: "GOLD III"

    actual_score = db.Column(db.Integer)  # 관리자 입력 실제 점수

    # 포지션 정보 ("TOP", "JUNGLE", "MID", "ADC", "SUPPORT")
    primary_role = db.Column(db.String(16))
    secondary_role1 = db.Column(db.String(16)) 
    secondary_role2 = db.Column(db.String(16)) 

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    participants = db.relationship("Participant", back_populates="user")


class Tournament(db.Model):
    __tablename__ = "tournaments"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)

    type = db.Column(db.String(16), default="KNOCKOUT") # 대회 방식: "KNOCKOUT", "LEAGUE", "LEAGUE_FINAL"
    status = db.Column(db.String(32), default="OPEN")  # OPEN, IN_PROGRESS, FINISHED
    current_stage = db.Column(db.String(16), default="LEAGUE") # 현재 단계: LEAGUE, PLAYOFF, FINAL 등
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    participants = db.relationship("Participant", back_populates="tournament")
    teams = db.relationship("Team", back_populates="tournament")


class Participant(db.Model):
    __tablename__ = "participants"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)
    status = db.Column(db.String(16), default="PENDING")  # PENDING, APPROVED, REJECTED
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User", back_populates="participants")
    tournament = db.relationship("Tournament", back_populates="participants")
    team_membership = db.relationship("TeamMember", back_populates="participant", uselist=False)


class Team(db.Model):
    __tablename__ = "teams"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)
    captain_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))

    tournament = db.relationship("Tournament", back_populates="teams")
    members = db.relationship("TeamMember", back_populates="team")
    captain = db.relationship("User", foreign_keys=[captain_user_id])


class TeamMember(db.Model):
    __tablename__ = "team_members"

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    participant_id = db.Column(db.Integer, db.ForeignKey("participants.id"), nullable=False)

    # 새로 추가: 팀 자동 구성 시 이 멤버가 어떤 포지션으로 배정되었는지
    assigned_role = db.Column(db.String(16))  # 예: "TOP", "JUNGLE", ...

    team = db.relationship("Team", back_populates="members")
    participant = db.relationship("Participant", back_populates="team_membership")

class Match(db.Model):
    __tablename__ = "matches"

    id = db.Column(db.Integer, primary_key=True)
    tournament_id = db.Column(db.Integer, db.ForeignKey("tournaments.id"), nullable=False)

    # 라운드/단계
    stage = db.Column(db.String(16), default="LEAGUE")  # LEAGUE / PLAYOFF / FINAL
    round_no = db.Column(db.Integer)    # 라운드 번호(1,2,3,...)
    match_no = db.Column(db.Integer)    # 해당 라운드 내 경기 번호

    team1_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    team2_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)

    scheduled_at = db.Column(db.DateTime)

    status = db.Column(db.String(16), default="SCHEDULED")  # SCHEDULED / DONE
    team1_score = db.Column(db.Integer)
    team2_score = db.Column(db.Integer)
    winner_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"))

    tournament = db.relationship("Tournament")
    team1 = db.relationship("Team", foreign_keys=[team1_id])
    team2 = db.relationship("Team", foreign_keys=[team2_id])
    winner_team = db.relationship("Team", foreign_keys=[winner_team_id])