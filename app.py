import math
from flask import Flask, render_template, request, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
from itertools import combinations
from datetime import datetime, timedelta

from config import Config
from models import db, User, Tournament, Participant, Team, TeamMember, Match
from riot_api import get_summoner_ranks
from score import calculate_estimated_score


ROLES = ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"]


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

    with app.app_context():
        db.create_all()

        # 필요하다면 기본 관리자 계정 생성 (예: admin / admin)
        if db.session.get(User, 1) is None:
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
    user = db.session.get(User, user_id)
    
    return user

# 🚨 추가: 토너먼트 ID를 기반으로 객체를 가져오는 헬퍼 함수
def get_tournament_or_404(tournament_id):
    t = db.session.get(Tournament, tournament_id)
    if not t:
        from flask import abort
        abort(404, description="Tournament not found")
    return t


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
                flash("권한이 거부되었습니다. 현재 권한은 " + active_role + "입니다.")
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
    # NOTE: calculate_estimated_score is imported from score.py
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


def update_user_riot_ranks(user: User):
    """
    유저의 Riot ID를 사용하여 최신 랭크 정보를 조회하고 User 객체를 업데이트합니다.
    (최근 24시간 이내에 갱신된 경우 API 호출을 건너뜁니다.)
    """
    if not user.summoner_riot_id:
        return

    # 🚨 수정된 로직: 24시간 Rate Limiting 체크
    if user.last_rank_update_at and (datetime.now() - user.last_rank_update_at) < timedelta(hours=24):
        # 24시간이 지나지 않았으므로 갱신을 건너뜁니다.
        return
        
    solo_tier, flex_tier, puuid = get_summoner_ranks(user.summoner_riot_id)
    
    needs_commit = False
    
    # Riot ID가 설정되어 있지만 puuid가 없을 경우를 대비하여 갱신합니다.
    if user.puuid != puuid:
        user.puuid = puuid
        needs_commit = True
        
    # 솔로 랭크 티어 갱신
    if user.solo_tier != solo_tier:
        user.solo_tier = solo_tier
        needs_commit = True

    # 자유 랭크 티어 갱신
    if user.flex_tier != flex_tier:
        user.flex_tier = flex_tier
        needs_commit = True
        
    # tier 필드 업데이트 (솔로 우선, 없으면 플렉스)
    new_tier = None
    if solo_tier:
        new_tier = solo_tier
    elif flex_tier:
        new_tier = flex_tier
        
    if user.tier != new_tier:
        user.tier = new_tier
        needs_commit = True
    
    if needs_commit:
        # 갱신이 발생한 경우에만 갱신 시각을 기록
        user.last_rank_update_at = datetime.now()
        db.session.commit()
    elif user.puuid:
        # 갱신은 필요 없지만 API 호출에 성공한 경우, 타임스탬프만 업데이트하여 API 호출 주기 초기화
        user.last_rank_update_at = datetime.now()
        db.session.commit()


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
             pass 

    elif tournament.type == "LEAGUE":
        # 리그전의 경우, calculate_league_standings를 사용합니다.
        standings = calculate_league_standings(tournament)
        if standings:
            return db.session.get(Team, standings[0]['team_id'])

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


def tournament_history_data_loader(tournament):
    # Matches
    matches = Match.query.filter_by(tournament_id=tournament.id).order_by(Match.stage, Match.round_no, Match.match_no).all()
    
    # Teams and Scores Calculation
    teams = Team.query.filter_by(tournament_id=tournament.id).all()
    
    for team in teams:
        base_total = 0
        weighted_total = 0
        captain_user = None
        for tm in team.members:
            user = tm.participant.user
            base = get_user_score(user)
            w_score = get_member_weighted_score(user, tm.assigned_role)

            base_total += base
            weighted_total += w_score

            # Attach scores dynamically for template access
            tm.base_score = base
            tm.weighted_score = w_score
            
            # Attach captain's user object
            # Note: team.captain_user_id는 TeamMember 객체의 user가 아닌
            # User 객체 자체에서 가져와야 하지만, 편의상 여기서 찾아서 team 객체에 붙여줍니다.
            if team.captain_user_id == user.id:
                 captain_user = user 
        
        team.base_total = base_total
        team.weighted_total = weighted_total
        team.captain_user = captain_user # 팀 객체에 캡틴 유저 정보 첨부
        
    # Winner
    winner_team = calculate_tournament_winner(tournament)
        
    # League Standings
    league_standings = None
    if tournament.type in ('LEAGUE', 'LEAGUE_FINAL'):
        league_standings = calculate_league_standings(tournament) 
        
    return {
        "tournament": tournament,
        "matches": matches,
        "teams": teams,
        "winner_team": winner_team,
        "league_standings": league_standings
    }


def calculate_theoretical_rounds(tournament_id):
    """
    Calculates the total number of rounds required for a complete knockout bracket
    based on the number of teams. N_rounds = ceil(log2(N_teams)).
    """
    teams_count = Team.query.filter_by(tournament_id=tournament_id).count()
    if teams_count < 2:
        return 0
    return math.ceil(math.log2(teams_count))


@app.context_processor
def inject_helpers():
    return dict(
        calculate_estimated_score=calculate_estimated_score,
        get_user_score=get_user_score,
        get_member_weighted_score=get_member_weighted_score,
        max=max,
    )


# -------------------- Auth --------------------

@app.route("/", methods=["GET"])
def login_page():
    # 최초 진입 화면 = 로그인 화면
    # NOTE: 현재 로그인 세션이 유지되고 있다면 dashboard로 리디렉션하는 로직이 필요할 수 있습니다.
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
            flash("권한 선택을 위한 사용자를 찾을 수 없습니다.")
            return redirect(url_for("login_page"))
        
        # 🚨 추가/수정: 승인 상태 확인 (2단계)
        if user.approval_status != "APPROVED":
            # 🚨 Rejected 상태 처리 (2단계)
            if user.approval_status == "REJECTED":
                username_to_delete = user.username
                # 메시지를 보여주기 위해 flash 후 계정 삭제
                db.session.delete(user)
                db.session.commit()
                flash(f"로그인에 실패했습니다. 회원님의 계정 등록 요청이 거부되어 영구적으로 삭제되었습니다. 다시 신청하려면 재등록해주세요.")
                return redirect(url_for("login_page"))
                
            # PENDING 상태: 대기 메시지
            flash(f"로그인에 실패했습니다. 회원님의 계정 상태는 '{user.approval_status}'입니다. 관리자 승인을 기다려주세요.")
            return redirect(url_for("login_page"))
            
        all_roles = [r.strip() for r in user.role.split(',') if r.strip()]

        if selected_role not in all_roles:
            flash("유효하지 않은 권한 선택입니다.")
            return redirect(url_for("login_page"))
            
        active_role = selected_role
        
    # Case 2: Initial Login (First Step - Username/Password)
    else:
        # 1단계: 인증 실패
        if not user or not check_password_hash(user.password_hash, password):
            flash("유효하지 않은 아이디 또는 비밀번호입니다.")
            return redirect(url_for("login_page"))

        # 🚨 추가/수정: 승인 상태 확인 (1단계)
        if user.approval_status != "APPROVED":
            # 🚨 Rejected 상태 처리 (1단계)
            if user.approval_status == "REJECTED":
                username_to_delete = user.username
                # 메시지를 보여주기 위해 flash 후 계정 삭제
                db.session.delete(user)
                db.session.commit()
                flash(f"로그인에 실패했습니다. 회원님의 계정 등록 요청이 거부되어 영구적으로 삭제되었습니다. 다시 신청하려면 재등록해주세요.")
                return redirect(url_for("login_page"))
                
            # PENDING 상태: 대기 메시지
            flash(f"로그인에 실패했습니다. 회원님의 계정 상태는 '{user.approval_status}'입니다. 관리자 승인을 기다려주세요.")
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
        flash("아이디와 비밀번호는 필수 입력 사항입니다.")
        return redirect(url_for("register"))

    if User.query.filter_by(username=username).first():
        flash("이미 존재하는 아이디입니다.")
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
    flash("회원가입이 완료되었습니다. 관리자 승인을 기다려주세요.")
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
    
    # 🚨 추가: 대시보드 접근 시 최신 랭크 정보 자동 업데이트
    update_user_riot_ranks(user)
    
    # 🚨 수정: 토너먼트 목록을 가져옵니다.
    tournaments = Tournament.query.all()
    
    # 🚨 추가: 사용자의 활성 참여 토너먼트 상태를 확인 (APPROVED + OPEN/IN_PROGRESS)
    active_participants = Participant.query.filter_by(
        user_id=user.id, 
        status="APPROVED"
    ).join(Tournament).filter(
        Tournament.status.in_(["OPEN", "IN_PROGRESS"])
    ).all()
    
    has_active_tournaments = len(active_participants) > 0 # 새로운 플래그
    
    # 대시보드에서는 신청 정보 대신 토너먼트 목록만 보여줍니다.
    # 신청 정보는 /user/tournaments 페이지로 이동합니다.

    return render_template(
        "user/dashboard.html",
        user=user,
        tournaments=tournaments, # 토너먼트 목록을 전달합니다.
        # 🚨 새로운 플래그 전달
        has_active_tournaments=has_active_tournaments, 
    )

@app.route("/user/profile", methods=["GET", "POST"])
@login_required(role="USER")
def user_profile():
    # (Profile Routes remain unchanged as they are not tournament-specific)
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
                flash("이미 다른 사용자가 사용 중인 학번입니다.")
                return redirect(url_for("user_profile"))

        # Riot ID 중복 체크 (원하면 유지)
        if riot_id:
            existing_riot = User.query.filter(
                User.summoner_riot_id == riot_id,
                User.id != user.id
            ).first()
            if existing_riot:
                flash("이미 다른 사용자가 사용 중인 Riot ID입니다.")
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

        flash("프로필이 업데이트되었습니다.")
        return redirect(url_for("user_profile"))

    return render_template("user/profile.html", user=user)

# 🚨 추가: 사용자용 토너먼트 목록 페이지
@app.route("/user/tournaments")
@login_required(role="USER")
def user_tournaments():
    # 🚨 수정된 핵심 로직: 모든 토너먼트를 가져와서 참여 여부와 상관없이 표시합니다.
    tournaments = Tournament.query.all()
    user = current_user()
    
    # 각 토너먼트에 대한 사용자의 참가 상태를 조회
    for t in tournaments:
        t.my_participant = Participant.query.filter_by(
            user_id=user.id, tournament_id=t.id
        ).first()

    return render_template("user/tournaments.html", tournaments=tournaments)


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/user/tournaments/<int:tournament_id>/apply", methods=["GET", "POST"])
@login_required(role="USER")
def user_apply(tournament_id):
    user = current_user()
    tournament = get_tournament_or_404(tournament_id)

    # GET 요청 처리 시 미리 확인하여 템플릿에 전달
    is_application_open = (tournament.status == "OPEN")
                
    # POST 요청 처리
    if request.method == "POST":
        # ----------------------------------------------------
        # 신청 가능 여부 확인
        # ----------------------------------------------------
        if tournament.status not in ("OPEN",):
            flash(f"신청이 마감되었습니다. 토너먼트 상태는 {tournament.status}입니다.")
            return redirect(url_for("user_apply", tournament_id=tournament_id))

        # 프로필 필수 정보 확인
        if not user.student_id or not user.real_name:
            flash("신청 전에 프로필에 학번과 이름을 입력해주세요.")
            return redirect(url_for("user_profile"))

        if not user.summoner_riot_id:
            flash("신청 전에 프로필에 Riot ID(닉네임#태그)를 설정해주세요.")
            return redirect(url_for("user_profile"))

        # 포지션 3개 모두 필수
        if not user.primary_role or not user.secondary_role1 or not user.secondary_role2:
            flash("신청 전에 프로필에 주/보조 포지션을 모두 설정해주세요.")
            return redirect(url_for("user_profile"))

        existing = Participant.query.filter_by(
            user_id=user.id, tournament_id=tournament.id
        ).first()

        if existing:
            flash(f"이미 신청하셨습니다. 현재 상태: {existing.status}")
            return redirect(url_for("user_apply", tournament_id=tournament_id))

        p = Participant(
            user_id=user.id,
            tournament_id=tournament.id,
            status="PENDING",
        )
        db.session.add(p)
        db.session.commit()

        flash("참가 신청이 완료되었습니다.")
        return redirect(url_for("user_apply", tournament_id=tournament_id))

    # GET 요청 처리
    current_participant = Participant.query.filter_by(
        user_id=user.id,
        tournament_id=tournament_id,
    ).first()

    return render_template(
        "user/apply.html",
        user=user,
        tournament=tournament,
        current_participant=current_participant,
        is_application_open=is_application_open,
    )


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/user/tournaments/<int:tournament_id>/team")
@login_required(role="USER")
def user_team(tournament_id):
    user = current_user()
    tournament = get_tournament_or_404(tournament_id)
    
    participant = Participant.query.filter_by(
        user_id=user.id, tournament_id=tournament.id
    ).first()
    team = None

    if participant and participant.team_membership:
        team = participant.team_membership.team

    if not team:
        return render_template("user/team.html", team=None, is_captain=False, tournament=tournament)

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

    return render_template("user/team.html", team=team, is_captain=is_captain, tournament=tournament)


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/user/tournaments/<int:tournament_id>/team/rename", methods=["POST"])
@login_required(role="USER")
def user_team_rename(tournament_id):
    user = current_user()
    tournament = get_tournament_or_404(tournament_id)
    
    participant = Participant.query.filter_by(
        user_id=user.id, tournament_id=tournament.id
    ).first()

    if not participant or not participant.team_membership:
        flash("팀에 배정되어 있지 않습니다.")
        return redirect(url_for("user_team", tournament_id=tournament_id))

    team = participant.team_membership.team

    if team.captain_user_id != user.id:
        flash("팀 주장만 팀 이름을 변경할 수 있습니다.")
        return redirect(url_for("user_team", tournament_id=tournament_id))

    new_name = request.form.get("team_name", "").strip()
    if not new_name:
        flash("팀 이름은 비워둘 수 없습니다.")
        return redirect(url_for("user_team", tournament_id=tournament_id))

    team.name = new_name
    db.session.commit()
    flash("팀 이름이 업데이트되었습니다.")
    return redirect(url_for("user_team", tournament_id=tournament_id))


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/user/tournaments/<int:tournament_id>/matches")
@login_required(role="USER")
def user_matches(tournament_id):
    user = current_user()
    tournament = get_tournament_or_404(tournament_id)
    
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


# (user_match_report는 match_id만으로 충분하며, 내부에서 tournament를 찾습니다.)
@app.route("/user/matches/<int:match_id>/report", methods=["GET", "POST"])
@login_required(role="USER")
def user_match_report(match_id):
    user = current_user()
    m = Match.query.get_or_404(match_id)
    tournament = get_tournament_or_404(m.tournament_id) # 🚨 수정: get_tournament_or_404 사용

    # 이 유저가 이 경기의 팀장인지 확인
    my_participant = Participant.query.filter_by(
        user_id=user.id, tournament_id=tournament.id
    ).first()
    if not my_participant or not my_participant.team_membership:
        flash("이 대회의 어떤 팀에도 소속되어 있지 않습니다.")
        return redirect(url_for("user_matches", tournament_id=tournament.id)) # 🚨 수정: 토너먼트 ID 전달

    my_team = my_participant.team_membership.team
    if my_team.id not in (m.team1_id, m.team2_id):
        flash("이 매치에 참가하는 선수가 아닙니다.")
        return redirect(url_for("user_matches", tournament_id=tournament.id)) # 🚨 수정: 토너먼트 ID 전달

    # 팀장 체크
    if my_team.captain_user_id != user.id:
        flash("팀 주장만 결과를 보고할 수 있습니다.")
        return redirect(url_for("user_matches", tournament_id=tournament.id)) # 🚨 수정: 토너먼트 ID 전달

    if request.method == "POST":
        try:
            s1 = int(request.form.get("team1_score", "0"))
            s2 = int(request.form.get("team2_score", "0"))
        except ValueError:
            flash("점수는 정수여야 합니다.")
            return redirect(url_for("user_match_report", match_id=match_id))
        
        # 경기 형식 가져오기: total_games, wins_needed, format_name
        bestof, wins_needed, format_name = get_match_format(m, tournament) 

        # BoX 형식에 맞는지 검증
        if s1 < wins_needed and s2 < wins_needed:
            # 승리 조건을 충족하지 못한 경우 (예: Bo3에서 1:1, Bo5에서 2:2)
            flash(f"{format_name}에 대한 유효하지 않은 점수입니다: 최소한 한 팀은 {wins_needed}승을 달성해야 합니다.")
            return redirect(url_for("user_match_report", match_id=match_id))
        
        # BoX 형식에 맞는지 검증
        if s1 + s2 > bestof:
            # 승리 조건을 충족하지 못한 경우 (예: Bo3에서 1:1, Bo5에서 2:2)
            flash(f"{format_name}에 대한 유효하지 않은 점수입니다: 총 매치 수는 {bestof}을 초과할 수 없습니다.")
            return redirect(url_for("user_match_report", match_id=match_id))
        
        # 둘 다 승리 조건을 충족할 수는 없음 (예: Bo3에서 2:2, Bo5에서 3:3)
        if s1 >= wins_needed and s2 >= wins_needed:
             # 일반적으로 이런 상황은 발생하지 않아야 하지만, 데이터 무결성을 위해 막음
             flash(f"{format_name}에 대한 유효하지 않은 점수입니다: 양 팀 모두 {wins_needed}승 이상을 달성할 수 없습니다.")
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

        flash("매치 결과가 제출되었습니다.")
        return redirect(url_for("user_matches", tournament_id=tournament.id)) # 🚨 수정: 토너먼트 ID 전달

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
    
    # 🚨 추가: 모든 Riot ID를 가진 유저의 랭크 정보 자동 업데이트
    all_users_with_riot_id = User.query.filter(User.summoner_riot_id.isnot(None)).all()
    for u in all_users_with_riot_id:
        # Note: update_user_riot_ranks handles the DB commit if needed.
        update_user_riot_ranks(u)

    # 1. 시스템 전체 사용자 및 승인 대기 사용자 수 계산
    total_users_count = User.query.count()
    pending_users_count = User.query.filter_by(approval_status="PENDING").count()

    # 🚨 수정: 토너먼트 목록을 가져옵니다.
    tournaments = Tournament.query.all()
    
    # 대시보드에서 각 토너먼트의 요약 정보를 계산하여 표시
    for t in tournaments:
        t.total_applications = Participant.query.filter_by(tournament_id=t.id).count()
        t.approved = Participant.query.filter_by(tournament_id=t.id, status="APPROVED").count()
        t.total_teams_count = Team.query.filter_by(tournament_id=t.id).count()
        t.pending_matches_count = Match.query.filter_by(tournament_id=t.id, status="SCHEDULED").count()
    
    return render_template(
        "admin/dashboard.html",
        tournaments=tournaments, # 🚨 수정: tournaments 목록 전달
        # 추가된 시스템 개요 변수
        total_users_count=total_users_count,
        pending_users_count=pending_users_count,
    )

# 🚨 추가: 관리자용 토너먼트 목록/생성 페이지
@app.route("/admin/tournaments", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_tournaments():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        t_type = request.form.get("type", "KNOCKOUT").strip()
        
        if not name:
            flash("대회 이름은 필수입니다.")
        elif t_type not in ("KNOCKOUT", "LEAGUE", "LEAGUE_FINAL"):
            flash("유효하지 않은 대회 유형입니다.")
        else:
            t = Tournament(
                name=name,
                type=t_type,
                status="OPEN"
            )
            db.session.add(t)
            db.session.commit()
            flash(f"대회 '{name}'이(가) 생성되었습니다.")
            return redirect(url_for("admin_tournaments"))

    tournaments = Tournament.query.all()
    return render_template("admin/tournaments.html", tournaments=tournaments)


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/admin/tournaments/<int:tournament_id>/participants")
@login_required(role="ADMIN")
def admin_participants(tournament_id):
    tournament = get_tournament_or_404(tournament_id) # 🚨 수정: get_tournament_or_404 사용
    participants = Participant.query.filter_by(
        tournament_id=tournament.id
    ).all()
    return render_template("admin/participants.html", participants=participants, tournament=tournament)


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/admin/tournaments/<int:tournament_id>/participants/<int:participant_id>/approve", methods=["POST"])
@login_required(role="ADMIN")
def admin_approve_participant(tournament_id, participant_id):
    tournament = get_tournament_or_404(tournament_id) # 🚨 추가
    p = db.session.get(Participant, participant_id)
    if not p:
        from flask import abort
        abort(404, description="Participant not found")

    # 🚨 추가: 토너먼트 ID 일치 확인
    if p.tournament_id != tournament.id:
        flash("참가자가 이 대회에 소속되어 있지 않습니다.")
        return redirect(url_for("admin_participants", tournament_id=tournament_id))

    # 이미 처리된 신청이면 그대로 반환
    if p.status != "PENDING":
        flash("이 참가자는 PENDING(승인 대기) 상태가 아닙니다.")
        return redirect(url_for("admin_participants", tournament_id=tournament_id))

    score_key = f"input_score_{participant_id}"
    input_score_str = request.form.get(score_key, "").strip()

    # 1) 입력 점수가 있으면 그 값으로 승인 시도
    if input_score_str:
        try:
            actual = int(input_score_str)
        except ValueError:
            flash("유효하지 않은 점수입니다. 정수를 입력해주세요.")
            return redirect(url_for("admin_participants", tournament_id=tournament_id))

        p.user.actual_score = actual
        p.status = "APPROVED"
        db.session.commit()
        flash("입력된 점수로 승인되었습니다.")
        return redirect(url_for("admin_participants", tournament_id=tournament_id))

    # 2) 입력 점수가 없으면 Estimated Score 사용
    est = calculate_estimated_score(p.user)

    if est is not None:
        p.user.actual_score = est
        p.status = "APPROVED"
        db.session.commit()
        flash("예상 점수로 승인되었습니다.")
    else:
        # Estimated score도 없으면 승인하지 않음
        flash("입력된 점수와 예상 점수 모두 없어 승인이 건너뛰어졌습니다.")

    return redirect(url_for("admin_participants", tournament_id=tournament_id))


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/admin/tournaments/<int:tournament_id>/participants/<int:participant_id>/reject", methods=["POST"])
@login_required(role="ADMIN")
def admin_reject_participant(tournament_id, participant_id):
    tournament = get_tournament_or_404(tournament_id) # 🚨 추가
    p = db.session.get(Participant, participant_id)
    if not p:
        from flask import abort
        abort(404, description="Participant not found")
    
    if p.tournament_id != tournament.id:
        flash("참가자가 이 대회에 소속되어 있지 않습니다.")
        return redirect(url_for("admin_participants", tournament_id=tournament_id))

    p.status = "REJECTED"
    db.session.commit()
    return redirect(url_for("admin_participants", tournament_id=tournament_id))


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/admin/tournaments/<int:tournament_id>/participants/bulk-approve", methods=["POST"])
@login_required(role="ADMIN")
def admin_bulk_approve_participants(tournament_id):
    tournament = get_tournament_or_404(tournament_id) # 🚨 추가
    id_list = request.form.getlist("participant_id")

    approved_count = 0
    skipped_count = 0

    for pid in id_list:
        try:
            participant_id = int(pid)
        except ValueError:
            continue

        p = db.session.get(Participant, participant_id)
        if not p or p.status != "PENDING" or p.tournament_id != tournament.id: # 🚨 추가: 토너먼트 ID 일치 확인
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
    flash(f"일괄 승인이 완료되었습니다. 승인: {approved_count}명, 건너뜀 (점수 없음): {skipped_count}명")
    return redirect(url_for("admin_participants", tournament_id=tournament_id))


# 🚨 추가: 승인된 참가자의 점수 수정
@app.route("/admin/tournaments/<int:tournament_id>/participants/<int:participant_id>/score", methods=["POST"])
@login_required(role="ADMIN")
def admin_update_approved_score(tournament_id, participant_id):
    tournament = get_tournament_or_404(tournament_id)
    p = db.session.get(Participant, participant_id)
    
    if not p:
        from flask import abort
        abort(404, description="Participant not found")
        
    # 1. 토너먼트 ID 및 승인 상태 확인
    if p.tournament_id != tournament.id or p.status != "APPROVED":
        flash("유효하지 않은 요청: 참가자가 이 대회에 승인되지 않았습니다.")
        return redirect(url_for("admin_participants", tournament_id=tournament_id))
        
    new_score_str = request.form.get("actual_score", "").strip()
    
    if not new_score_str:
        flash("점수 입력란을 비워둘 수 없습니다.")
        return redirect(url_for("admin_participants", tournament_id=tournament_id))
        
    try:
        new_score = int(new_score_str)
    except ValueError:
        flash("유효하지 않은 점수입니다. 정수를 입력해주세요.")
        return redirect(url_for("admin_participants", tournament_id=tournament_id))

    # 2. User의 actual_score 업데이트
    p.user.actual_score = new_score
    db.session.commit()
    
    flash(f"사용자 '{p.user.real_name or p.user.username}'의 실제 점수가 {new_score}로 업데이트되었습니다.")
    return redirect(url_for("admin_participants", tournament_id=tournament_id))


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/admin/tournaments/<int:tournament_id>/teams", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_teams(tournament_id):
    tournament = get_tournament_or_404(tournament_id) # 🚨 수정: get_tournament_or_404 사용
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
        flash("팀 정보가 업데이트되었습니다.")
        return redirect(url_for("admin_teams", tournament_id=tournament_id))

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

    return render_template("admin/teams.html", teams=teams, tournament=tournament) # 🚨 수정: 토너먼트 전달


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/admin/tournaments/<int:tournament_id>/teams/auto-generate", methods=["POST"])
@login_required(role="ADMIN")
def admin_auto_generate_teams(tournament_id):
    tournament = get_tournament_or_404(tournament_id) # 🚨 수정: get_tournament_or_404 사용

    # 기존 팀/팀원 삭제
    TeamMember.query.filter(TeamMember.team.has(tournament_id=tournament.id)).delete(synchronize_session=False)
    Team.query.filter_by(tournament_id=tournament.id).delete(synchronize_session=False)
    Match.query.filter_by(tournament_id=tournament.id).delete(synchronize_session=False) # 🚨 추가: 매치도 같이 삭제
    db.session.commit()

    # 승인된 참가자만 사용
    approved = Participant.query.filter_by(
        tournament_id=tournament.id, status="APPROVED"
    ).all()

    if not approved:
        flash("팀을 생성할 승인된 참가자가 없습니다.")
        return redirect(url_for("admin_teams", tournament_id=tournament_id))

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
        flash("유효한 포지션을 가진 선수가 없어 팀을 생성할 수 없습니다.")
        return redirect(url_for("admin_teams", tournament_id=tournament_id))

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
        flash("최소한 한 팀(5인, 전 포지션)을 구성하기에 충분한 선수가 없습니다.")
        return redirect(url_for("admin_teams", tournament_id=tournament_id))

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
    flash("팀이 생성되었습니다.")
    return redirect(url_for("admin_teams", tournament_id=tournament_id))


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
            flash(f"사용자 {target.username}의 권한이 다음과 같이 변경되었습니다: {target.role}")
            
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
        flash(f"사용자 {user.username}이(가) 회원 가입 승인되었습니다.")
    # 🚨 수정: 거부 시 상태만 REJECTED로 변경 (삭제하지 않음)
    elif action == "reject":
        user.approval_status = "REJECTED"
        db.session.commit()
        flash(f"사용자 {user.username}의 회원 가입 요청이 거부되었습니다. 다음 로그인 시도 시 계정이 삭제됩니다.")
    # 🚨 추가: 관리자가 수동으로 영구 삭제
    elif action == "delete":
        username = user.username
        # 🚨 데이터베이스에서 사용자 영구 삭제
        db.session.delete(user)
        db.session.commit()
        flash(f"사용자 {username}이(가) 영구적으로 삭제되었습니다.")
    else:
        flash("유효하지 않은 액션입니다.")

    return redirect(url_for("admin_user_approvals"))


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/admin/tournaments/<int:tournament_id>", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_tournament(tournament_id):
    tournament = get_tournament_or_404(tournament_id) # 🚨 수정: get_tournament_or_404 사용

    if request.method == "POST":
        t_type = request.form.get("type", "").strip()
        status = request.form.get("status", "").strip()
        
        # 🚨 수정된 로직: Status가 OPEN일 때만 Type 변경 허용
        if t_type and t_type in ("KNOCKOUT", "LEAGUE", "LEAGUE_FINAL"):
            
            if tournament.status == "OPEN":
                tournament.type = t_type
            elif tournament.type != t_type:
                flash("대회 유형은 상태가 OPEN일 때만 변경할 수 있습니다.")

        status = request.form.get("status", "").strip()
        if status in ("OPEN", "IN_PROGRESS", "FINISHED"):
            tournament.status = status

        db.session.commit()
        flash("대회 설정이 업데이트되었습니다.")
        return redirect(url_for("admin_tournament", tournament_id=tournament_id))

    # 현재 생성된 경기들
    matches = Match.query.filter_by(tournament_id=tournament.id).order_by(
        Match.stage, Match.round_no, Match.match_no
    ).all()

    # 🚨 수정: 이론적 라운드 수 계산
    total_rounds = calculate_theoretical_rounds(tournament_id)

    # 리그 순위 계산 (LEAGUE 또는 LEAGUE_FINAL 일 때)
    league_standings = None
    if tournament.type in ("LEAGUE", "LEAGUE_FINAL"):
        league_standings = calculate_league_standings(tournament)
        
    # FINISHED 상태일 때 우승팀 정보를 가져옵니다.
    winner_team = calculate_tournament_winner(tournament)

    return render_template("admin/tournament.html", 
        tournament=tournament, 
        matches=matches,
        winner_team=winner_team,
        league_standings=league_standings,
        total_rounds=total_rounds, # 🚨 새로운 변수 전달
    )


# 🚨 수정: 토너먼트 ID를 인수로 받도록 변경
@app.route("/admin/tournaments/<int:tournament_id>/generate_schedule", methods=["POST"])
@login_required(role="ADMIN")
def admin_generate_schedule(tournament_id):
    tournament = get_tournament_or_404(tournament_id) # 🚨 수정: get_tournament_or_404 사용

    # 기존 Match 삭제
    Match.query.filter_by(tournament_id=tournament.id).delete()
    db.session.commit()

    teams = Team.query.filter_by(tournament_id=tournament.id).all()
    if len(teams) < 2:
        flash("일정을 생성하려면 최소 2개 팀이 필요합니다.")
        return redirect(url_for("admin_tournament", tournament_id=tournament_id))

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
    flash("일정이 생성되었습니다.")
    return redirect(url_for("admin_tournament", tournament_id=tournament_id))


# (admin_match_report는 match_id만으로 충분하며, 내부에서 tournament를 찾습니다.)
@app.route("/admin/matches/<int:match_id>/report", methods=["GET", "POST"])
@login_required(role="ADMIN") # 관리자 권한만 확인
def admin_match_report(match_id):
    # 매치 객체를 'm' 변수에 가져옵니다.
    m = Match.query.get_or_404(match_id)
    tournament = get_tournament_or_404(m.tournament_id) # 🚨 수정: get_tournament_or_404 사용

    # 관리자 라우트이므로, 팀 소속이나 팀장 여부를 확인하지 않습니다.

    if request.method == "POST":
        try:
            # 폼에서 점수를 가져옵니다.
            s1 = int(request.form.get("team1_score", "0"))
            s2 = int(request.form.get("team2_score", "0"))
        except ValueError:
            flash("점수는 정수여야 합니다.")
            return redirect(url_for("admin_match_report", match_id=match_id))

        # 경기 형식 가져오기: total_games, wins_needed, format_name
        bestof, wins_needed, format_name = get_match_format(m, tournament) 

        # BoX 형식에 맞는지 검증
        if s1 < wins_needed and s2 < wins_needed:
            # 승리 조건을 충족하지 못한 경우 (예: Bo3에서 1:1, Bo5에서 2:2)
            flash(f"{format_name}에 대한 유효하지 않은 점수입니다: 최소한 한 팀은 {wins_needed}승을 달성해야 합니다.")
            return redirect(url_for("admin_match_report", match_id=match_id))
        
        # BoX 형식에 맞는지 검증
        if s1 + s2 > bestof:
            # 승리 조건을 충족하지 못한 경우 (예: Bo3에서 1:1, Bo5에서 2:2)
            flash(f"{format_name}에 대한 유효하지 않은 점수입니다: 총 매치 수는 {bestof}을 초과할 수 없습니다.")
            return redirect(url_for("admin_match_report", match_id=match_id))

        if s1 >= wins_needed and s2 >= wins_needed:
             # 둘 다 승리 조건을 충족할 수는 없음
             flash(f"{format_name}에 대한 유효하지 않은 점수입니다: 양 팀 모두 {wins_needed}승 이상을 달성할 수 없습니다.")
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

        flash("매치 결과가 수정되었습니다.")
        # 업데이트 후 관리자 토너먼트 상세 페이지로 리다이렉트
        return redirect(url_for("admin_tournament", tournament_id=tournament.id)) # 🚨 수정: 토너먼트 ID 전달

    # GET 요청 시, 기존 사용자 템플릿(score 입력 폼)을 재사용하고 'match'로 전달합니다.
    return render_template("admin/match_report.html", match=m)


# 🚨 새로운 라우트 이름: tournament_history
@app.route("/tournament/<int:tournament_id>/history")
@login_required() # 모든 로그인 사용자에게 접근 허용
def tournament_history(tournament_id):
    tournament = get_tournament_or_404(tournament_id) 

    data = tournament_history_data_loader(tournament)

    data['total_rounds'] = calculate_theoretical_rounds(tournament_id)
    
    # 템플릿 렌더링
    return render_template(
        "tournament_history.html",
        **data 
    )


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)