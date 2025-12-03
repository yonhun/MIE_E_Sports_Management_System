from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

from config import Config
from models import db, User, Tournament, Participant, Team, TeamMember, Match
from itertools import combinations

from riot_api import get_summoner_ranks
from score import calculate_estimated_score

import random

ROLES = ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"]

def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    with app.app_context():
        db.create_all()

        # 기본 토너먼트 1개 생성
        if Tournament.query.count() == 0:
            t = Tournament(name="Default Tournament")
            db.session.add(t)
            db.session.commit()

        # 필요하다면 기본 관리자 계정 생성 (예: admin / admin)
        if User.query.filter_by(username="admin").first() is None:
            admin_user = User(
                username="admin",
                password_hash=generate_password_hash("admin"),
                role="ADMIN",
                # 🚨 추가: 초기 관리자 계정은 즉시 승인 상태여야 합니다.
                approval_status="APPROVED", 
            )
            db.session.add(admin_user)
            db.session.commit()

    return app


app = create_app()

# -------------------- Helper --------------------

def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    user = User.query.get(user_id)
    
    return user


def login_required(role=None):
    def decorator(fn):
        from functools import wraps

        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            # 🚨 수정: 세션에서 활성 역할(active role)을 가져옵니다.
            active_role = session.get("role") 
            
            if not user or not active_role:
                return redirect(url_for("login_page"))
            
            # 🚨 수정: 세션의 활성 역할이 필요한 역할과 일치하는지 확인
            if role and active_role != role:
                flash("Permission denied. You are logged in as " + active_role + ".")
                return redirect(url_for("login_page"))
            
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def get_user_score(user) -> int:
    """
    사용자 점수:
      - actual_score가 있으면 그것 사용
      - 없으면 estimated score 사용
      - 둘 다 없으면 0
    """
    if user.actual_score is not None:
        return user.actual_score
    est = calculate_estimated_score(user)
    if est is not None:
        return est
    return 0


def get_member_weight(user, assigned_role: str | None) -> float:
    """
    팀에 배정된 포지션(assigned_role)에 따라 weight 계산
      - primary_role 이면 1.0
      - secondary_role1 이면 0.9
      - secondary_role2 이면 0.8
      - 그 외/없음은 1.0
    """
    if not assigned_role:
        return 1.0

    if assigned_role == user.primary_role:
        return 1.0
    if assigned_role == user.secondary_role1:
        return 0.9
    if assigned_role == user.secondary_role2:
        return 0.8
    return 1.0


def get_member_weighted_score(user, assigned_role: str | None) -> float:
    base = get_user_score(user)
    w = get_member_weight(user, assigned_role)
    return base * w


def get_match_format(m: Match, tournament: Tournament):
    """
    매치의 스테이지와 토너먼트 타입을 기반으로 Bo3 또는 Bo5 형식을 결정합니다.
    - LEAGUE 스테이지: Bo3 (2승 선취)
    - PLAYOFF/FINAL 스테이지: Bo5 (3승 선취)
    """
    if m.stage == "LEAGUE":
        # 리그전은 Bo3
        return 3, 2, "Bo3" # Total games, Wins needed, Format name
    if m.stage in ("PLAYOFF", "FINAL"):
        # 플레이오프/결승은 Bo5
        return 5, 3, "Bo5"
    # 기본값 (혹은 정의되지 않은 경우)
    return 1, 1, "Single Match"


def progress_tournament_if_needed(tournament: Tournament):
    if tournament.type == "KNOCKOUT":
        # ... [KNOCKOUT 로직은 그대로 유지] ...
        max_round = db.session.query(db.func.max(Match.round_no)).filter_by(
            tournament_id=tournament.id,
            stage="PLAYOFF"
        ).scalar()
        if not max_round:
            return

        # 해당 라운드의 모든 경기 DONE ?
        current_round_matches = Match.query.filter_by(
            tournament_id=tournament.id,
            stage="PLAYOFF",
            round_no=max_round,
        ).all()
        
        # 1. 현재 라운드 경기가 모두 DONE인지 확인
        if current_round_matches and any(m.status != "DONE" for m in current_round_matches):
            return # 아직 완료되지 않은 경기가 있음

        # 2. 다음 라운드 진출 팀 목록 (승자 + 부전승)
        winners = []
        
        # 2-1. 현재 라운드 승자 추가
        for m in current_round_matches:
             if m.winner_team_id:
                # winner_team_id가 있는 경우만 추가
                winners.append(m.winner_team)
                
        # 2-2. 부전승 팀 추가 (첫 라운드일 경우만)
        if max_round == 1:
            # 첫 라운드에 매치가 없었던 팀 = 부전승 팀
            # 모든 팀 ID
            all_team_ids = {t.id for t in Team.query.filter_by(tournament_id=tournament.id).all()}
            # 현재 라운드에 참여한 팀 ID
            playing_team_ids = {m.team1_id for m in current_round_matches} | {m.team2_id for m in current_round_matches}
            # 부전승 팀 ID = 전체 팀 - 참여 팀
            bye_team_ids = all_team_ids - playing_team_ids
            
            # 부전승 팀 객체를 winners에 추가
            bye_teams = Team.query.filter(Team.id.in_(bye_team_ids)).all()
            winners.extend(bye_teams)


        # 3. 우승 결정 여부 확인
        # 1팀만 남았거나 0팀이면 종료
        if len(winners) <= 1:
            tournament.status = "FINISHED"
            db.session.commit()
            return

        # 4. 다음 라운드 생성
        next_round = max_round + 1
        match_no = 1
        
        # Seed 유지: winners를 ID 순서로 정렬 (Seed 로직이 없으므로 임시)
        winners.sort(key=lambda t: t.id) 

        
        # 4-1. 다음 라운드 매치 페어링
        # 팀 수가 홀수일 경우, 마지막 팀은 다음 라운드의 부전승이 되어 자동으로 진행됨.
        for i in range(0, len(winners), 2):
            t1 = winners[i]
            
            # 다음 팀이 없으면 (홀수), 현재 팀은 다음 라운드의 부전승이 됨.
            if i + 1 >= len(winners):
                # 마지막 남은 팀은 다음 라운드 진출 (매치 생성 X)
                break 
                
            t2 = winners[i+1]
            
            # 매치 생성
            m = Match(
                tournament_id=tournament.id,
                stage="PLAYOFF",
                round_no=next_round,
                match_no=match_no,
                team1_id=t1.id,
                team2_id=t2.id,
                status="SCHEDULED",
            )
            db.session.add(m)
            match_no += 1
            
        db.session.commit()

    # ----------------------------------------------------
    # [수정된 부분]: LEAGUE 및 LEAGUE_FINAL 완료 처리
    # ----------------------------------------------------
    if tournament.type in ("LEAGUE", "LEAGUE_FINAL") and tournament.status == "IN_PROGRESS":
        
        # 1. 모든 리그전(LEAGUE stage) 경기가 완료되었는지 확인
        league_matches = Match.query.filter_by(tournament_id=tournament.id, stage="LEAGUE").all()
        
        # League 경기가 'DONE' 상태이면서 동시에 '무승부'가 아닌지 확인합니다.
        # (승패가 명확해야 완료된 경기로 간주)
        def is_match_really_done(m: Match):
            # 상태가 DONE이면서, 점수 기록이 있어야 하며, 점수가 달라야 함 (무승부 배제)
            return (m.status == "DONE" and 
                    m.team1_score is not None and m.team2_score is not None and 
                    m.team1_score != m.team2_score)

        all_league_done = all(is_match_really_done(m) for m in league_matches)
        
        if all_league_done:
            
            if tournament.type == "LEAGUE_FINAL":
                # 2. LEAGUE_FINAL: 리그가 끝나면 결승전(FINAL stage)을 생성하거나 완료 확인
                final_match = Match.query.filter_by(tournament_id=tournament.id, stage="FINAL").first()
                
                if not final_match:
                    # 결승전이 없으면 상위 2팀을 찾아 결승전 매치 생성
                    standings = calculate_league_standings(tournament)
                    if len(standings) >= 2:
                        # calculate_league_standings는 승점/득실차 순으로 정렬된 리스트를 반환합니다.
                        t1_id = standings[0]['team_id']
                        t2_id = standings[1]['team_id']
                        
                        final_match = Match(
                            tournament_id=tournament.id,
                            stage="FINAL",
                            round_no=1, # 결승전은 보통 1라운드
                            match_no=1,
                            team1_id=t1_id,
                            team2_id=t2_id,
                            status="SCHEDULED",
                        )
                        db.session.add(final_match)
                        db.session.commit()
                        # 상태는 IN_PROGRESS 유지 (결승전이 남아있으므로)
                
                # 3. LEAGUE_FINAL: 결승전이 완료되면 FINISHED로 변경
                elif final_match.status == "DONE":
                    tournament.status = "FINISHED"
                    db.session.commit()
                    
            elif tournament.type == "LEAGUE":
                # 4. 순수 LEAGUE: 모든 리그전이 완료되면 FINISHED로 변경
                # 리그 1위 팀이 우승팀이 됩니다.
                tournament.status = "FINISHED"
                db.session.commit()


def calculate_tournament_winner(tournament: Tournament) -> Team | None:
    """
    토너먼트의 최종 우승팀을 찾습니다.
    - KNOCKOUT/LEAGUE_FINAL: 가장 높은 라운드/스테이지의 승자
    - LEAGUE: 승점이 가장 높은 팀
    """
    if tournament.status != "FINISHED":
        return None

    if tournament.type in ("KNOCKOUT", "LEAGUE_FINAL"):
        # 플레이오프/파이널의 마지막 매치를 찾습니다.
        final_match = Match.query.filter_by(
            tournament_id=tournament.id,
        ).filter(
            (Match.stage == "FINAL") | (Match.stage == "PLAYOFF")
        ).order_by(
            Match.round_no.desc(), Match.match_no.desc()
        ).first()

        if final_match and final_match.winner_team_id:
            return final_match.winner_team
        
        # 4팀 이하시 부전승으로 끝난 경우 (예: 1팀만 남음)
        if tournament.type == "KNOCKOUT":
             # 마지막 라운드 승자 목록 (최종 우승팀만 남았을 가능성)
             # 이 로직은 progress_tournament_if_needed가 제대로 작동하면 필요 없을 수 있지만, 안전 장치입니다.
             pass # 현재 로직에서는 progress_tournament_if_needed가 FINISHED 상태로 만들 때 winner_team을 찾지 않으므로, 이 부분을 개선해야 함.

    elif tournament.type == "LEAGUE":
        # 리그전의 경우, calculate_league_standings를 사용합니다.
        standings = calculate_league_standings(tournament)
        if standings:
            return Team.query.get(standings[0]['team_id'])

    return None

def calculate_league_standings(tournament: Tournament) -> list:
    """
    LEAGUE 스테이지의 승점을 계산하여 순위 리스트를 반환합니다.
    (승 3점, 패 0점. 무승부(Draw)는 Bo3 포맷에서 제외됨)
    순위 기준: 1. 총 승점(P), 2. 게임 득실차(GD)
    """
    league_matches = Match.query.filter_by(tournament_id=tournament.id, stage="LEAGUE").all()
    teams = Team.query.filter_by(tournament_id=tournament.id).all()
    
    stats = {}
    for team in teams:
        stats[team.id] = {
            'team_id': team.id,
            'team_name': team.name,
            'P': 0, # Points
            'W': 0, # Wins
            'L': 0, # Losses
            'GD': 0, # Game Differential (Games For - Games Against)
            'GF': 0, # Games For
            'GA': 0, # Games Against
        }

    for m in league_matches:
        t1_id = m.team1_id
        t2_id = m.team2_id
        
        # 점수가 없거나, 승리 조건(Bo3에서는 2승)을 만족하지 못해 1:1인 경우 무시 (미완료 경기)
        # Note: 유효한 Bo3 승리 점수는 2:0, 2:1, 0:2, 1:2 입니다.
        if m.team1_score is None or m.team2_score is None:
            continue
            
        s1 = m.team1_score
        s2 = m.team2_score

        # Bo3 승리 조건: 2승 선취이므로, 두 팀 모두 2승 미만이면 미완료/무효 처리
        if s1 < 2 and s2 < 2:
             continue # 1:1, 0:0 등은 계산에서 제외
        
        # 득실차 및 게임 수 업데이트
        stats[t1_id]['GF'] += s1
        stats[t1_id]['GA'] += s2
        stats[t2_id]['GF'] += s2
        stats[t2_id]['GA'] += s1
        
        # 승패/승점 업데이트
        if s1 > s2:
            # Team 1 승리
            stats[t1_id]['P'] += 3
            stats[t1_id]['W'] += 1
            stats[t2_id]['L'] += 1
        else: # s2 > s1 (s1 == s2 인 경우는 이미 위에서 제외됨)
            # Team 2 승리
            stats[t2_id]['P'] += 3
            stats[t2_id]['W'] += 1
            stats[t1_id]['L'] += 1

    # 득실차 (GD) 계산
    for team_id in stats:
        stats[team_id]['GD'] = stats[team_id]['GF'] - stats[team_id]['GA']
            
    # 순위 결정 기준: 1. 승점(P) 내림차순, 2. 득실차(GD) 내림차순
    sorted_standings = sorted(stats.values(), key=lambda x: (x['P'], x['GD']), reverse=True)
    return sorted_standings


def generate_league_round_robin(tournament, teams):
    round_no = 1
    match_no = 1
    # 매우 단순하게 조합으로 만들고, 쭉 늘어놓고 일정 순서만 round_no++
    for (t1, t2) in combinations(teams, 2):
        m = Match(
            tournament_id=tournament.id,
            stage="LEAGUE",
            round_no=round_no,
            match_no=match_no,
            team1_id=t1.id,
            team2_id=t2.id,
            status="SCHEDULED",
        )
        db.session.add(m)
        match_no += 1
        if match_no > 4:  # 임의 기준: 4경기마다 라운드 넘김 (원하면 조정)
            round_no += 1
            match_no = 1


def generate_knockout_initial_round(tournament, teams):
    """
    팀 수가 2의 거듭제곱이 아닐 경우 시드 배정 원칙을 적용하여 대진표를 생성합니다.
    (팀 객체에 score 속성이 없으므로, 일단 팀 ID를 점수 기준으로 가정하고 정렬합니다.)
    """
    
    num_teams = len(teams)
    
    if num_teams < 2:
        return

    # 1. 시드 배정 (점수가 높은 팀이 낮은 숫자의 시드를 받도록 정렬)
    # 현재 팀 객체에 직접 점수 속성이 없으므로, Team ID를 기준으로 임시 정렬합니다.
    # **실제 구현 시, 팀의 'weighted_total' 점수를 기준으로 정렬해야 합니다.**
    # 임시: ID 내림차순 정렬 (높은 ID = 높은 Seed라 가정)
    teams.sort(key=lambda t: t.id, reverse=True) 
    
    # 2. 다음 2의 거듭제곱 찾기 (라운드 크기 결정)
    n = 1
    while n < num_teams:
        n *= 2
    
    next_power_of_two = n
    
    # 3. 부전승(Bye) 수 계산
    num_byes = next_power_of_two - num_teams
    
    # 4. 첫 라운드(Round 1)에서 실제로 경기하는 팀 수
    num_playing_teams = num_teams - num_byes
    
    # 5. 시드 기반 배치
    # Seed 1 ~ num_teams 까지의 팀을 대진표 위치에 따라 배치합니다.
    
    # 예시: 5팀 (8자리 대진) -> 3팀 부전승 (Bye), 2팀 경기
    # 표준 시드 배치: [S1] [S8] vs [S7] | [S4] vs [S5] | [S3] [S6]
    # 실제 5팀: [S1] [B] [S2] vs [S5] | [S3] vs [S4] | [B] [B]
    
    # 부전승 팀: 가장 높은 시드 팀(Seed 1, 2, 3...)에게 배정합니다.
    bye_teams = teams[:num_byes]
    playing_teams = teams[num_byes:] # 나머지 팀이 경기를 합니다.
    
    # 6. 경기할 팀 쌍 생성 및 표준 대진표 순서 적용
    matches = []
    
    # 경기할 팀(playing_teams)을 '대진표' 상의 표준 페어링 순서에 맞게 재배열
    # 예: 4팀이 경기한다면 (S1/S2/S3/S4), 보통 S1 vs S4, S2 vs S3 이 이상적입니다.
    
    match_pairs = []
    
    # 경기하는 팀 수가 짝수이므로, 절반으로 나눠 높은 시드와 낮은 시드를 페어링
    # S(num_byes + 1) vs S(num_teams)
    # S(num_byes + 2) vs S(num_teams - 1) ...
    
    half_playing = len(playing_teams) // 2
    
    for i in range(half_playing):
        t1 = playing_teams[i]                       # 높은 시드 그룹 (앞쪽)
        t2 = playing_teams[len(playing_teams) - 1 - i] # 낮은 시드 그룹 (뒷쪽)
        match_pairs.append((t1, t2))
    
    # 경기 순서를 위해 match_pairs를 평탄화 (필요시)
    
    round_no = 1
    match_no = 1
    
    # 7. 첫 라운드 매치 생성
    for t1, t2 in match_pairs:
        m = Match(
            tournament_id=tournament.id,
            stage="PLAYOFF",
            round_no=round_no,
            match_no=match_no,
            team1_id=t1.id,
            team2_id=t2.id,
            status="SCHEDULED",
        )
        db.session.add(m)
        match_no += 1
        
    # 부전승 팀은 'progress_tournament_if_needed'에서 자동으로 다음 라운드 진출 처리됩니다.


@app.context_processor
def inject_helpers():
    return dict(
        calculate_estimated_score=calculate_estimated_score,
        get_user_score=get_user_score,
        get_member_weighted_score=get_member_weighted_score,
    )


# -------------------- Auth --------------------

@app.route("/", methods=["GET"])
def login_page():
    # 최초 진입 화면 = 로그인 화면
    # 🚨 수정: GET 요청 시 역할 선택 폼이 표시되지 않도록 빈 roles=None을 전달
    return render_template("login.html", roles=None)


@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username")
    password = request.form.get("password")
    selected_role = request.form.get("selected_role")

    user = User.query.filter_by(username=username).first()

    # Case 1: Role Selection is being submitted (Second Step)
    if selected_role:
        if not user:
            flash("User not found for role selection.")
            return redirect(url_for("login_page"))
        
        # 🚨 추가/수정: 승인 상태 확인 (2단계)
        if user.approval_status != "APPROVED":
            # 🚨 Rejected 상태 처리 (2단계)
            if user.approval_status == "REJECTED":
                username_to_delete = user.username
                # 메시지를 보여주기 위해 flash 후 계정 삭제
                db.session.delete(user)
                db.session.commit()
                flash(f"Login failed. Your account registration was rejected and has been permanently deleted. Please register again if you wish to re-apply.")
                return redirect(url_for("login_page"))
                
            # PENDING 상태: 대기 메시지
            flash(f"Login failed. Your account status is: {user.approval_status}. Waiting for Admin approval.")
            return redirect(url_for("login_page"))
            
        all_roles = [r.strip() for r in user.role.split(',') if r.strip()]

        if selected_role not in all_roles:
            flash("Invalid role selection.")
            return redirect(url_for("login_page"))
            
        active_role = selected_role
        
    # Case 2: Initial Login (First Step - Username/Password)
    else:
        # 1단계: 인증 실패
        if not user or not check_password_hash(user.password_hash, password):
            flash("Invalid username or password")
            return redirect(url_for("login_page"))

        # 🚨 추가/수정: 승인 상태 확인 (1단계)
        if user.approval_status != "APPROVED":
            # 🚨 Rejected 상태 처리 (1단계)
            if user.approval_status == "REJECTED":
                username_to_delete = user.username
                # 메시지를 보여주기 위해 flash 후 계정 삭제
                db.session.delete(user)
                db.session.commit()
                flash(f"Login failed. Your account registration was rejected and has been permanently deleted. Please register again if you wish to re-apply.")
                return redirect(url_for("login_page"))
                
            # PENDING 상태: 대기 메시지
            flash(f"Login failed. Your account status is: {user.approval_status}. Waiting for Admin approval.")
            return redirect(url_for("login_page"))
        
        # 1단계: 인증 성공 (APPROVED)
        all_roles = [r.strip() for r in user.role.split(',') if r.strip()]
        
        if len(all_roles) > 1:
            # 다중 역할 사용자: 역할 선택 폼으로 전환 (2단계 준비)
            return render_template(
                "login.html", 
                username=username, 
                roles=all_roles
            )
        else:
            # 단일 역할 사용자: 바로 로그인 처리
            active_role = all_roles[0] if all_roles else "USER"
            

    # Final session setting and redirect
    if 'active_role' in locals():
        session["user_id"] = user.id
        session["role"] = active_role

        if active_role == "ADMIN":
            return redirect(url_for("admin_dashboard"))
        else:
            return redirect(url_for("user_dashboard"))
    
    return redirect(url_for("login_page"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    username = request.form.get("username")
    password = request.form.get("password")
    initial_role_selection = request.form.get("initial_role", "USER").upper()

    if not username or not password:
        flash("Username and password are required.")
        return redirect(url_for("register"))

    if User.query.filter_by(username=username).first():
        flash("Username already exists.")
        return redirect(url_for("register"))

    role_to_save = initial_role_selection
    
    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        role=role_to_save, 
        # 🚨 추가: 사용자 계정 상태를 PENDING으로 설정
        approval_status="PENDING", 
    )
    db.session.add(user)
    db.session.commit()

    # 🚨 수정: 승인 대기 메시지로 변경
    flash("Registration submitted. Your account is pending administrator approval.")
    return redirect(url_for("login_page"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login_page"))


# -------------------- User Routes --------------------

@app.route("/user/dashboard")
@login_required(role="USER")
def user_dashboard():
    user = current_user()
    tournament = Tournament.query.first()
    participant = None
    my_team = None  # 팀 정보를 담을 변수 초기화

    if tournament:
        participant = Participant.query.filter_by(
            user_id=user.id, tournament_id=tournament.id
        ).first()
        
        # 참가자가 있고 팀 멤버십이 있다면 팀 정보를 가져옴
        if participant and participant.team_membership:
            my_team = participant.team_membership.team

    return render_template(
        "user/dashboard.html",
        user=user,
        tournament=tournament,
        participant=participant,
        my_team=my_team, # <-- my_team 변수 추가 전달
    )


@app.route("/user/profile", methods=["GET", "POST"])
@login_required(role="USER")
def user_profile():
    user = current_user()

    if request.method == "POST":
        student_id = request.form.get("student_id", "").strip()
        real_name = request.form.get("real_name", "").strip()
        riot_id = request.form.get("riot_id", "").strip()

        primary_role = request.form.get("primary_role", "").strip()
        secondary_role1 = request.form.get("secondary_role1", "").strip()
        secondary_role2 = request.form.get("secondary_role2", "").strip()

        # 학번 중복 체크
        if student_id:
            existing = User.query.filter(
                User.student_id == student_id,
                User.id != user.id
            ).first()
            if existing:
                flash("This student ID is already used by another user.")
                return redirect(url_for("user_profile"))

        # Riot ID 중복 체크 (원하면 유지)
        if riot_id:
            existing_riot = User.query.filter(
                User.summoner_riot_id == riot_id,
                User.id != user.id
            ).first()
            if existing_riot:
                flash("This Riot ID is already used by another user.")
                return redirect(url_for("user_profile"))

        user.student_id = student_id or None
        user.real_name = real_name or None
        user.summoner_riot_id = riot_id or None

        # 포지션 저장
        user.primary_role = primary_role or None
        user.secondary_role1 = secondary_role1 or None
        user.secondary_role2 = secondary_role2 or None

        # Riot ID 기반 티어/점수 업데이트 (기존 로직 유지)
        user.tier = None
        user.solo_tier = None
        user.flex_tier = None
        user.puuid = None

        if riot_id:
            solo_tier, flex_tier, puuid = get_summoner_ranks(riot_id)
            user.solo_tier = solo_tier
            user.flex_tier = flex_tier
            user.puuid = puuid

            if solo_tier:
                user.tier = solo_tier
            elif flex_tier:
                user.tier = flex_tier

        db.session.commit()

        flash("Profile updated.")
        return redirect(url_for("user_profile"))

    return render_template("user/profile.html", user=user)


@app.route("/user/apply", methods=["GET", "POST"])
@login_required(role="USER")
def user_apply():
    user = current_user()
    tournaments = Tournament.query.all()

    selected_tournament_id = None
    if tournaments:
        selected_tournament_id = tournaments[0].id

    # 토너먼트 상태를 GET 요청 처리 시 미리 확인하여 템플릿에 전달
    current_tournament = None
    is_application_open = False
    
    if selected_tournament_id:
        current_tournament = Tournament.query.get(selected_tournament_id)
        if current_tournament:
            # 신청 가능 조건: OPEN 상태
            if current_tournament.status == "OPEN":
                is_application_open = True
                
    # POST 요청 처리
    if request.method == "POST":
        selected_tournament_id = request.form.get("tournament_id")
        if not selected_tournament_id:
            flash("Tournament selection is required.")
            return redirect(url_for("user_apply"))

        tournament = Tournament.query.get(selected_tournament_id)
        if not tournament:
            flash("Invalid tournament.")
            return redirect(url_for("user_apply"))

        # ----------------------------------------------------
        # 신청 가능 여부 확인
        # ----------------------------------------------------
        if tournament.status not in ("OPEN",):
            flash(f"Application is closed. The tournament is currently {tournament.status}.")
            return redirect(url_for("user_apply"))
        # ----------------------------------------------------

        # 프로필 필수 정보 확인
        if not user.student_id or not user.real_name:
            flash("Please fill in your student ID and name in the profile before applying.")
            return redirect(url_for("user_profile"))

        if not user.summoner_riot_id:
            flash("Please set your Riot ID (nickname#tag) in the profile before applying.")
            return redirect(url_for("user_profile"))

        # 포지션 3개 모두 필수
        if not user.primary_role or not user.secondary_role1 or not user.secondary_role2:
            flash("Please set your primary and secondary roles in the profile before applying.")
            return redirect(url_for("user_profile"))

        existing = Participant.query.filter_by(
            user_id=user.id, tournament_id=tournament.id
        ).first()

        if existing:
            flash(f"You have already applied. Current status: {existing.status}")
            return redirect(url_for("user_apply"))

        p = Participant(
            user_id=user.id,
            tournament_id=tournament.id,
            status="PENDING",
        )
        db.session.add(p)
        db.session.commit()

        flash("Application submitted.")
        return redirect(url_for("user_apply"))

    # GET 요청 처리
    current_participant = None
    if selected_tournament_id:
        current_participant = Participant.query.filter_by(
            user_id=user.id,
            tournament_id=selected_tournament_id,
        ).first()

    return render_template(
        "user/apply.html",
        user=user,
        tournaments=tournaments,
        selected_tournament_id=selected_tournament_id,
        current_participant=current_participant,
        is_application_open=is_application_open, # 템플릿에 전달
        current_tournament=current_tournament, # 템플릿에 전달
    )


@app.route("/user/team")
@login_required(role="USER")
def user_team():
    user = current_user()
    tournament = Tournament.query.first()
    participant = None
    team = None

    if tournament:
        participant = Participant.query.filter_by(
            user_id=user.id, tournament_id=tournament.id
        ).first()
        if participant and participant.team_membership:
            team = participant.team_membership.team

    if not team:
        return render_template("user/team.html", team=None, is_captain=False)

    is_captain = (team.captain_user_id == user.id)

    # 팀 점수 계산은 기존대로
    base_total = 0
    weighted_total = 0
    for tm in team.members:
        u = tm.participant.user
        base = get_user_score(u)
        w_score = get_member_weighted_score(u, tm.assigned_role)
        base_total += base
        weighted_total += w_score
        tm.base_score = base
        tm.weighted_score = w_score
    team.base_total = base_total
    team.weighted_total = weighted_total

    return render_template("user/team.html", team=team, is_captain=is_captain)


@app.route("/user/team/rename", methods=["POST"])
@login_required(role="USER")
def user_team_rename():
    user = current_user()
    tournament = Tournament.query.first()
    if not tournament:
        flash("No tournament found.")
        return redirect(url_for("user_team"))

    participant = Participant.query.filter_by(
        user_id=user.id, tournament_id=tournament.id
    ).first()

    if not participant or not participant.team_membership:
        flash("You are not assigned to any team.")
        return redirect(url_for("user_team"))

    team = participant.team_membership.team

    if team.captain_user_id != user.id:
        flash("Only the team captain can rename the team.")
        return redirect(url_for("user_team"))

    new_name = request.form.get("team_name", "").strip()
    if not new_name:
        flash("Team name cannot be empty.")
        return redirect(url_for("user_team"))

    team.name = new_name
    db.session.commit()
    flash("Team name updated.")
    return redirect(url_for("user_team"))


@app.route("/user/tournament-status")
@login_required(role="USER")
def user_tournament_status():
    tournament = Tournament.query.first()
    teams = []
    league_standings = None
    matches = []
    winner_team = None

    if tournament:
        teams = Team.query.filter_by(tournament_id=tournament.id).all()
        
        if tournament.type in ("LEAGUE", "LEAGUE_FINAL"):
            league_standings = calculate_league_standings(tournament)
        
        if tournament.type == "KNOCKOUT":
            # 녹아웃은 매치 목록을 보여줍니다.
            matches = Match.query.filter_by(tournament_id=tournament.id).order_by(
                Match.round_no.desc(), Match.match_no.desc()
            ).all()

        if tournament.status == "FINISHED":
            winner_team = calculate_tournament_winner(tournament)

    return render_template(
        "user/tournament_status.html",
        tournament=tournament,
        teams=teams,
        league_standings=league_standings, # 템플릿에 전달
        matches=matches,                   # 템플릿에 전달
        winner_team=winner_team,           # 템플릿에 전달
    )


@app.route("/user/matches")
@login_required(role="USER")
def user_matches():
    user = current_user()
    # 이 유저가 속한 팀들 (보통 하나지만 일반화)
    team_ids = {tm.team_id for p in Participant.query.filter_by(user_id=user.id).all()
                               for tm in p.team_membership.team.members} if False else set()

    # 실제로는 좀 더 간단히: 현재 토너먼트 기준에서 participant → team 찾기
    tournament = Tournament.query.first()
    
    # 1. 토너먼트가 없는 경우
    if not tournament:
        return render_template("user/matches.html", matches=[], tournament=None, my_participant=None, my_team_id=None)

    my_participant = Participant.query.filter_by(
        user_id=user.id, tournament_id=tournament.id
    ).first()
    
    my_team_id = None
    if my_participant and my_participant.team_membership:
        my_team_id = my_participant.team_membership.team_id

    # 2. 유저가 참가하지 않았거나 팀에 배정되지 않은 경우
    if not my_team_id:
        return render_template("user/matches.html", 
                               matches=[], 
                               tournament=tournament, 
                               my_participant=my_participant,
                               my_team_id=None)

    matches = Match.query.filter(
        Match.tournament_id == tournament.id,
        ((Match.team1_id == my_team_id) | (Match.team2_id == my_team_id))
    ).order_by(Match.stage, Match.round_no, Match.match_no).all()

    return render_template("user/matches.html", 
                           matches=matches, 
                           tournament=tournament, 
                           my_participant=my_participant, 
                           my_team_id=my_team_id)


@app.route("/user/matches/<int:match_id>/report", methods=["GET", "POST"])
@login_required(role="USER")
def user_match_report(match_id):
    user = current_user()
    m = Match.query.get_or_404(match_id)
    tournament = Tournament.query.get(m.tournament_id)

    # 이 유저가 이 경기의 팀장인지 확인
    my_participant = Participant.query.filter_by(
        user_id=user.id, tournament_id=tournament.id
    ).first()
    if not my_participant or not my_participant.team_membership:
        flash("You are not in any team for this tournament.")
        return redirect(url_for("user_matches"))

    my_team = my_participant.team_membership.team
    if my_team.id not in (m.team1_id, m.team2_id):
        flash("You are not a player in this match.")
        return redirect(url_for("user_matches"))

    # 팀장 체크
    if my_team.captain_user_id != user.id:
        flash("Only the team captain can report results.")
        return redirect(url_for("user_matches"))

    if request.method == "POST":
        try:
            s1 = int(request.form.get("team1_score", "0"))
            s2 = int(request.form.get("team2_score", "0"))
        except ValueError:
            flash("Scores must be integers.")
            return redirect(url_for("user_match_report", match_id=match_id))
        
        # 경기 형식 가져오기: total_games, wins_needed, format_name
        bestof, wins_needed, format_name = get_match_format(m, tournament) 

        # BoX 형식에 맞는지 검증
        if s1 < wins_needed and s2 < wins_needed:
            # 승리 조건을 충족하지 못한 경우 (예: Bo3에서 1:1, Bo5에서 2:2)
            flash(f"Invalid score for {format_name}: At least one team must reach {wins_needed} wins.")
            return redirect(url_for("user_match_report", match_id=match_id))
        
        # BoX 형식에 맞는지 검증
        if s1 + s2 > bestof:
            # 승리 조건을 충족하지 못한 경우 (예: Bo3에서 1:1, Bo5에서 2:2)
            flash(f"Invalid score for {format_name}: Total match count cannot exceed {bestof}.")
            return redirect(url_for("user_match_report", match_id=match_id))
        
        # 둘 다 승리 조건을 충족할 수는 없음 (예: Bo3에서 2:2, Bo5에서 3:3)
        if s1 >= wins_needed and s2 >= wins_needed:
             # 일반적으로 이런 상황은 발생하지 않아야 하지만, 데이터 무결성을 위해 막음
             flash(f"Invalid score for {format_name}: Both teams cannot meet or exceed {wins_needed} wins.")
             return redirect(url_for("user_match_report", match_id=match_id))


        m.team1_score = s1
        m.team2_score = s2
        m.status = "DONE"

        if s1 > s2:
            m.winner_team_id = m.team1_id
        elif s2 > s1:
            m.winner_team_id = m.team2_id
        else:
            m.winner_team_id = None  # 무승부 허용 여부에 따라 처리

        db.session.commit()

        # 다음 단계 진행 여부 체크 (KO / LEAGUE_FINAL 등)
        progress_tournament_if_needed(tournament)

        flash("Match result submitted.")
        return redirect(url_for("user_matches"))

    return render_template("user/match_report.html", match=m)


# -------------------- Team Routes ---------------------


@app.route("/team/<int:team_id>")
@login_required()  # USER, ADMIN 모두 접근 가능
def team_detail(team_id):
    team = Team.query.get_or_404(team_id)
    # 점수 합계 계산
    base_total = 0
    weighted_total = 0
    for tm in team.members:
        u = tm.participant.user
        base = get_user_score(u)
        w_score = get_member_weighted_score(u, tm.assigned_role)
        base_total += base
        weighted_total += w_score
        tm.base_score = base
        tm.weighted_score = w_score
    team.base_total = base_total
    team.weighted_total = weighted_total

    return render_template("team/detail.html", team=team)


# -------------------- Admin Routes --------------------

@app.route("/admin/dashboard")
@login_required(role="ADMIN")
def admin_dashboard():
    # 1. 시스템 전체 사용자 및 승인 대기 사용자 수 계산
    total_users_count = User.query.count()
    pending_users_count = User.query.filter_by(approval_status="PENDING").count()

    tournament = Tournament.query.first()
    total_applications = 0
    approved = 0
    
    # 템플릿에서 오류가 나지 않도록 기본값 초기화
    total_teams_count = 0
    pending_matches_count = 0
    
    if tournament:
        total_applications = Participant.query.filter_by(
            tournament_id=tournament.id
        ).count()
        approved = Participant.query.filter_by(
            tournament_id=tournament.id, status="APPROVED"
        ).count()
        
        # 2. 현재 토너먼트의 팀 및 매치 수 계산
        total_teams_count = Team.query.filter_by(tournament_id=tournament.id).count()
        pending_matches_count = Match.query.filter_by(
            tournament_id=tournament.id, status="SCHEDULED"
        ).count()

    return render_template(
        "admin/dashboard.html",
        tournament=tournament,
        total_applications=total_applications,
        approved=approved,
        # 추가된 시스템 개요 변수
        total_users_count=total_users_count,
        pending_users_count=pending_users_count,
        # 토너먼트 상태 테이블 변수
        total_teams_count=total_teams_count,
        pending_matches_count=pending_matches_count,
    )


@app.route("/admin/participants")
@login_required(role="ADMIN")
def admin_participants():
    tournament = Tournament.query.first()
    participants = []
    if tournament:
        participants = Participant.query.filter_by(
            tournament_id=tournament.id
        ).all()
    return render_template("admin/participants.html", participants=participants)


@app.route("/admin/participants/<int:participant_id>/approve", methods=["POST"])
@login_required(role="ADMIN")
def admin_approve_participant(participant_id):
    p = Participant.query.get_or_404(participant_id)

    # 이미 처리된 신청이면 그대로 반환
    if p.status != "PENDING":
        flash("This participant is not in PENDING status.")
        return redirect(url_for("admin_participants"))

    score_key = f"input_score_{participant_id}"
    input_score_str = request.form.get(score_key, "").strip()

    # 1) 입력 점수가 있으면 그 값으로 승인 시도
    if input_score_str:
        try:
            actual = int(input_score_str)
        except ValueError:
            flash("Invalid score. Please input an integer.")
            return redirect(url_for("admin_participants"))

        p.user.actual_score = actual
        p.status = "APPROVED"
        db.session.commit()
        flash("Approved with input score.")
        return redirect(url_for("admin_participants"))

    # 2) 입력 점수가 없으면 Estimated Score 사용
    est = calculate_estimated_score(p.user)

    if est is not None:
        p.user.actual_score = est
        p.status = "APPROVED"
        db.session.commit()
        flash("Approved with estimated score.")
    else:
        # Estimated score도 없으면 승인하지 않음
        flash("No input score and no estimated score; approval skipped.")

    return redirect(url_for("admin_participants"))


@app.route("/admin/participants/<int:participant_id>/reject", methods=["POST"])
@login_required(role="ADMIN")
def admin_reject_participant(participant_id):
    p = Participant.query.get_or_404(participant_id)
    p.status = "REJECTED"
    db.session.commit()
    return redirect(url_for("admin_participants"))


@app.route("/admin/participants/bulk-approve", methods=["POST"])
@login_required(role="ADMIN")
def admin_bulk_approve_participants():
    id_list = request.form.getlist("participant_id")

    approved_count = 0
    skipped_count = 0

    for pid in id_list:
        try:
            participant_id = int(pid)
        except ValueError:
            continue

        p = Participant.query.get(participant_id)
        if not p or p.status != "PENDING":
            continue

        score_key = f"input_score_{participant_id}"
        input_score_str = request.form.get(score_key, "").strip()

        # 1) 입력 점수가 있으면 그 값으로 승인 시도
        if input_score_str:
            try:
                actual = int(input_score_str)
            except ValueError:
                # 숫자가 아니면 이 참가자는 스킵
                skipped_count += 1
                continue

            p.user.actual_score = actual
            p.status = "APPROVED"
            approved_count += 1
            continue

        # 2) 입력 점수가 없으면 Estimated Score 사용
        est = calculate_estimated_score(p.user)

        if est is not None:
            p.user.actual_score = est
            p.status = "APPROVED"
            approved_count += 1
        else:
            # Estimated Score도 없으면 승인하지 않음
            skipped_count += 1

    db.session.commit()
    flash(f"Bulk approve finished. Approved: {approved_count}, skipped (no score): {skipped_count}")
    return redirect(url_for("admin_participants"))


@app.route("/admin/teams", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_teams():
    tournament = Tournament.query.first()
    if not tournament:
        flash("No tournament found.")
        return redirect(url_for("admin_dashboard"))

    teams = Team.query.filter_by(tournament_id=tournament.id).all()

    if request.method == "POST":
        for team in teams:
            # 팀명 수정
            name_key = f"team_name_{team.id}"
            new_name = request.form.get(name_key, "").strip()
            if new_name:
                team.name = new_name

            # 팀장 수정
            captain_key = f"captain_user_{team.id}"
            captain_id_str = request.form.get(captain_key, "").strip()
            if captain_id_str:
                try:
                    captain_id = int(captain_id_str)
                except ValueError:
                    continue

                # 해당 팀 멤버인지 확인 후 설정
                if any(tm.participant.user.id == captain_id for tm in team.members):
                    team.captain_user_id = captain_id

        db.session.commit()
        flash("Teams updated.")
        return redirect(url_for("admin_teams"))

    for team in teams:
        base_total = 0
        weighted_total = 0

        for tm in team.members:
            user = tm.participant.user
            base = get_user_score(user)                    # 기본 점수
            w_score = get_member_weighted_score(user, tm.assigned_role)  # 가중 점수

            base_total += base
            weighted_total += w_score

            # 팀원 개별 weighted score를 템플릿에서 쓰기 위해 주입
            tm.base_score = base
            tm.weighted_score = w_score

        # 팀 합계를 템플릿에서 쓰기 위해 주입
        team.base_total = base_total
        team.weighted_total = weighted_total

    return render_template("admin/teams.html", teams=teams)


@app.route("/admin/teams/auto-generate", methods=["POST"])
@login_required(role="ADMIN")
def admin_auto_generate_teams():
    tournament = Tournament.query.first()
    if not tournament:
        flash("No tournament found.")
        return redirect(url_for("admin_teams"))

    # 기존 팀/팀원 삭제
    TeamMember.query.delete()
    Team.query.filter_by(tournament_id=tournament.id).delete()
    db.session.commit()

    # 승인된 참가자만 사용
    approved = Participant.query.filter_by(
        tournament_id=tournament.id, status="APPROVED"
    ).all()

    if not approved:
        flash("No approved participants to generate teams.")
        return redirect(url_for("admin_teams"))

    # 포지션/점수 정보 준비
    players = []  # participant 단위 리스트
    for p in approved:
        u = p.user
        base_score = get_user_score(u)

        roles_weighted = []  # (role, weight)
        if u.primary_role in ROLES:
            roles_weighted.append((u.primary_role, 1.0))
        if u.secondary_role1 in ROLES and u.secondary_role1 != u.primary_role:
            roles_weighted.append((u.secondary_role1, 0.9))
        if u.secondary_role2 in ROLES and u.secondary_role2 not in [r for r, _ in roles_weighted]:
            roles_weighted.append((u.secondary_role2, 0.8))

        if not roles_weighted:
            # 포지션이 전혀 설정되지 않은 참가자는 팀 구성에서 제외
            continue

        players.append({
            "participant": p,
            "user": u,
            "base_score": base_score,
            "roles_weighted": roles_weighted,
        })

    if not players:
        flash("No players with valid roles to generate teams.")
        return redirect(url_for("admin_teams"))

    total_players = len(players)

    # 포지션별 후보 테이블 생성
    role_candidates = {role: [] for role in ROLES}
    for pl in players:
        for role, weight in pl["roles_weighted"]:
            effective_score = pl["base_score"] * weight
            role_candidates[role].append({
                "participant": pl["participant"],
                "user": pl["user"],
                "base_score": pl["base_score"],
                "effective_score": effective_score,
                "role": role,
            })

    # 각 포지션에 한 명씩 있어야 하므로, 해당 포지션 후보 수가 팀 수 상한을 결정
    max_by_count = total_players // 5
    max_by_roles = min(len(role_candidates[role]) for role in ROLES)

    team_count = min(max_by_count, max_by_roles)
    if team_count <= 0:
        flash("Not enough players to form at least one full team of 5 with all roles.")
        return redirect(url_for("admin_teams"))

    # 팀 데이터 구조 초기화
    teams_data = []
    for i in range(team_count):
        teams_data.append({
            "name": f"Team {i+1}",
            "members": [],       # list of dict: {"participant", "role", "base_score"}
            "total_score": 0,
        })

    assigned_participant_ids = set()

    # 1단계: 포지션별로 한 명씩 팀에 배치 (가중치 적용)
    for role in ROLES:
        candidates = role_candidates[role]
        # 가중치 점수(효과점수) 내림차순
        candidates.sort(key=lambda c: c["effective_score"], reverse=True)

        for cand in candidates:
            pid = cand["participant"].id
            if pid in assigned_participant_ids:
                continue

            # 현재 이 역할이 없는 팀 중, total_score가 낮은 팀부터 채움
            # 팀당 최대 5명 제한
            possible_teams = []
            for t in teams_data:
                # 이미 이 역할이 있는지
                has_role = any(m["role"] == role for m in t["members"])
                if has_role:
                    continue
                if len(t["members"]) >= 5:
                    continue
                possible_teams.append(t)

            if not possible_teams:
                # 이 역할을 더 이상 넣을 팀이 없음
                continue

            # 현재 total_score가 가장 낮은 팀 선택
            possible_teams.sort(key=lambda t: t["total_score"])
            team = possible_teams[0]

            team["members"].append({
                "participant": cand["participant"],
                "role": role,
                "base_score": cand["base_score"],
            })
            team["total_score"] += cand["base_score"]
            assigned_participant_ids.add(pid)

            # 모든 팀에 이 역할이 1명씩 배정되었으면 종료
            if all(any(m["role"] == role for m in t["members"]) for t in teams_data):
                break

    # 2단계: 아직 팀 인원이 5명 미만인 팀에 나머지 인원 채우기 (포지션 상관 없이 점수 밸런싱)
    remaining_players = [
        pl for pl in players
        if pl["participant"].id not in assigned_participant_ids
    ]

    # 점수 높은 순으로 남은 인원 정렬
    remaining_players.sort(key=lambda pl: pl["base_score"], reverse=True)

    for pl in remaining_players:
        # 아직 5명 미만인 팀만 대상
        not_full_teams = [t for t in teams_data if len(t["members"]) < 5]
        if not not_full_teams:
            break

        # 현재 total_score 가장 낮은 팀 선택
        not_full_teams.sort(key=lambda t: t["total_score"])
        team = not_full_teams[0]

        # 역할은 일단 주 포지션 우선, 없으면 "FLEX" 표시
        main_role = None
        for role, _w in pl["roles_weighted"]:
            if role in ROLES:
                main_role = role
                break
        if main_role is None:
            main_role = "FLEX"

        team["members"].append({
            "participant": pl["participant"],
            "role": main_role,
            "base_score": pl["base_score"],
        })
        team["total_score"] += pl["base_score"]
        assigned_participant_ids.add(pl["participant"].id)

    # 여기까지 하면 각 팀 인원 수는 최대 5명.
    # Team/TeamMember 실제 DB 생성
    for tdata in teams_data:
        # 1) 팀 내에서 base_score 최대인 사람 찾기
        captain_user_id = None
        max_score = -1
        for m in tdata["members"]:
            if m["base_score"] > max_score:
                max_score = m["base_score"]
                captain_user_id = m["participant"].user.id
        
        # 2) Team 생성 시 captain_user_id 설정
        team = Team(
            name=tdata["name"],
            tournament_id=tournament.id,
            captain_user_id=captain_user_id,
        )
        db.session.add(team)
        db.session.flush()  # team.id 확보

        # 3) 팀원 TeamMember 생성 (assigned_role 포함)
        for m in tdata["members"]:
            tm = TeamMember(
                team_id=team.id,
                participant_id=m["participant"].id,
                assigned_role=m["role"],
            )
            db.session.add(tm)

    db.session.commit()
    flash("Teams generated with role balancing and score heuristic (max 5 players per team).")
    return redirect(url_for("admin_teams"))


@app.route("/admin/permissions", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_permissions():
    users = User.query.all()
    if request.method == "POST":
        user_id = request.form.get("user_id")
        target = User.query.get(user_id)
        # 🚨 수정: 기존 역할에 ADMIN을 추가하고 저장
        if target:
            # 기존 역할을 파싱하여 set에 추가
            roles = set([r.strip() for r in target.role.split(',') if r.strip()])
            roles.add("ADMIN")
            target.role = ",".join(sorted(list(roles)))
            db.session.commit()
            flash(f"User {target.username} now has roles: {target.role}")
            
    return render_template("admin/permissions.html", users=users)


@app.route("/admin/user_approvals")
@login_required(role="ADMIN")
def admin_user_approvals():
    # PENDING 상태의 사용자 목록을 가져옵니다.
    pending_users = User.query.filter_by(approval_status="PENDING").all()
    # 🚨 수정: REJECTED 상태의 사용자 목록을 가져옵니다. (삭제되기 전)
    rejected_users = User.query.filter_by(approval_status="REJECTED").all()
    
    return render_template("admin/user_approvals.html", 
                           pending_users=pending_users,
                           rejected_users=rejected_users) # 템플릿에 전달


@app.route("/admin/user_approvals/<int:user_id>/<action>", methods=["POST"])
@login_required(role="ADMIN")
def admin_handle_user_approval(user_id, action):
    user = User.query.get_or_404(user_id)
    
    if action == "approve":
        user.approval_status = "APPROVED"
        db.session.commit()
        flash(f"User {user.username} approved.")
    # 🚨 수정: 거부 시 상태만 REJECTED로 변경 (삭제하지 않음)
    elif action == "reject":
        user.approval_status = "REJECTED"
        db.session.commit()
        flash(f"User {user.username}'s account request has been rejected. The account will be deleted upon their next login attempt.")
    # 🚨 추가: 관리자가 수동으로 영구 삭제
    elif action == "delete":
        username = user.username
        # 🚨 데이터베이스에서 사용자 영구 삭제
        db.session.delete(user)
        db.session.commit()
        flash(f"User {username} has been permanently deleted.")
    else:
        flash("Invalid action.")

    return redirect(url_for("admin_user_approvals"))


@app.route("/admin/tournament", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_tournament():
    tournament = Tournament.query.first()
    if not tournament:
        # 최초 1회 자동 생성
        tournament = Tournament(name="Main Tournament")
        db.session.add(tournament)
        db.session.commit()

    if request.method == "POST":
        t_type = request.form.get("type", "").strip()
        if t_type in ("KNOCKOUT", "LEAGUE", "LEAGUE_FINAL"):
            tournament.type = t_type

        status = request.form.get("status", "").strip()
        if status in ("OPEN", "IN_PROGRESS", "FINISHED"):
            tournament.status = status

        db.session.commit()
        flash("Tournament settings updated.")
        return redirect(url_for("admin_tournament"))

    # 현재 생성된 경기들
    matches = Match.query.filter_by(tournament_id=tournament.id).order_by(
        Match.stage, Match.round_no, Match.match_no
    ).all()

    # 리그 순위 계산 (LEAGUE 또는 LEAGUE_FINAL 일 때)
    league_standings = None
    if tournament.type in ("LEAGUE", "LEAGUE_FINAL"):
        league_standings = calculate_league_standings(tournament)
        
    # FINISHED 상태일 때 우승팀 정보를 가져옵니다.
    winner_team = calculate_tournament_winner(tournament)

    return render_template("admin/tournament.html", 
        tournament=tournament, 
        matches=matches,
        winner_team=winner_team, # 템플릿에 전달
        league_standings=league_standings, # 템플릿에 추가 전달
    )


@app.route("/admin/tournament/generate_schedule", methods=["POST"])
@login_required(role="ADMIN")
def admin_generate_schedule():
    tournament = Tournament.query.first()
    if not tournament:
        flash("No tournament found.")
        return redirect(url_for("admin_tournament"))

    # 기존 Match 삭제
    Match.query.filter_by(tournament_id=tournament.id).delete()
    db.session.commit()

    teams = Team.query.filter_by(tournament_id=tournament.id).all()
    if len(teams) < 2:
        flash("Need at least 2 teams to generate schedule.")
        return redirect(url_for("admin_tournament"))

    if tournament.type == "KNOCKOUT":
        generate_knockout_initial_round(tournament, teams)
        tournament.current_stage = "PLAYOFF"
    elif tournament.type == "LEAGUE":
        generate_league_round_robin(tournament, teams)
        tournament.current_stage = "LEAGUE"
    elif tournament.type == "LEAGUE_FINAL":
        generate_league_round_robin(tournament, teams)
        tournament.current_stage = "LEAGUE"

    tournament.status = "IN_PROGRESS"
    db.session.commit()
    flash("Schedule generated.")
    return redirect(url_for("admin_tournament"))


@app.route("/admin/matches/<int:match_id>/report", methods=["GET", "POST"])
@login_required(role="ADMIN") # 관리자 권한만 확인
def admin_match_report(match_id):
    # 매치 객체를 'm' 변수에 가져옵니다.
    m = Match.query.get_or_404(match_id)
    tournament = Tournament.query.get(m.tournament_id)

    # 관리자 라우트이므로, 팀 소속이나 팀장 여부를 확인하지 않습니다.

    if request.method == "POST":
        try:
            # 폼에서 점수를 가져옵니다.
            s1 = int(request.form.get("team1_score", "0"))
            s2 = int(request.form.get("team2_score", "0"))
        except ValueError:
            flash("Scores must be integers.")
            return redirect(url_for("admin_match_report", match_id=match_id))

        # 경기 형식 가져오기: total_games, wins_needed, format_name
        bestof, wins_needed, format_name = get_match_format(m, tournament) 

        # BoX 형식에 맞는지 검증
        if s1 < wins_needed and s2 < wins_needed:
            # 승리 조건을 충족하지 못한 경우 (예: Bo3에서 1:1, Bo5에서 2:2)
            flash(f"Invalid score for {format_name}: At least one team must reach {wins_needed} wins.")
            return redirect(url_for("admin_match_report", match_id=match_id))
        
        # BoX 형식에 맞는지 검증
        if s1 + s2 > bestof:
            # 승리 조건을 충족하지 못한 경우 (예: Bo3에서 1:1, Bo5에서 2:2)
            flash(f"Invalid score for {format_name}: Total match count cannot exceed {bestof}.")
            return redirect(url_for("admin_match_report", match_id=match_id))

        if s1 >= wins_needed and s2 >= wins_needed:
             # 둘 다 승리 조건을 충족할 수는 없음
             flash(f"Invalid score for {format_name}: Both teams cannot meet or exceed {wins_needed} wins.")
             return redirect(url_for("admin_match_report", match_id=match_id))


        # 매치 정보 업데이트
        m.team1_score = s1
        m.team2_score = s2
        m.status = "DONE" # 관리자가 수정한 경우 완료 처리

        if s1 > s2:
            m.winner_team_id = m.team1_id
        elif s2 > s1:
            m.winner_team_id = m.team2_id
        else:
            m.winner_team_id = None # 무승부

        db.session.commit()

        # 토너먼트 진행 (다음 라운드 매치 생성 등)
        progress_tournament_if_needed(tournament)

        flash("Match result updated successfully by Admin.")
        # 업데이트 후 관리자 메인 페이지로 리다이렉트
        return redirect(url_for("admin_tournament"))

    # GET 요청 시, 기존 사용자 템플릿(score 입력 폼)을 재사용하고 'match'로 전달합니다.
    return render_template("admin/match_report.html", match=m)


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)
