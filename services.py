import math
from datetime import datetime, timedelta
from itertools import combinations

from flask import abort
from models import db, User, Tournament, Match, Team, TeamMember, Participant
from riot_api import get_summoner_ranks
from score import calculate_estimated_score

# 포지션 상수
ROLES = ["TOP", "JUNGLE", "MID", "ADC", "SUPPORT"]

def get_tournament_or_404(tournament_id):
    """토너먼트 ID로 객체 조회, 실패 시 404"""
    t = db.session.get(Tournament, tournament_id)
    if not t:
        abort(404, description="Tournament not found")
    return t

def get_user_score(user) -> int:
    """사용자 점수 계산 (Actual > Estimated > 0)"""
    if user.actual_score is not None:
        return user.actual_score
    est = calculate_estimated_score(user)
    if est is not None:
        return est
    return 0

def get_member_weight(user, assigned_role: str | None) -> float:
    """포지션에 따른 가중치 반환"""
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
    """가중치가 적용된 최종 점수 반환"""
    base = get_user_score(user)
    w = get_member_weight(user, assigned_role)
    return base * w

def update_user_riot_ranks(user: User, force_update=False):
    """Riot API를 통해 유저 랭크 정보 갱신 (24시간 쿨타임 적용)"""
    if not user.summoner_riot_id:
        return

    if not force_update and user.last_rank_update_at and (datetime.now() - user.last_rank_update_at) < timedelta(hours=24):
        return
        
    solo_tier, flex_tier, puuid = get_summoner_ranks(user.summoner_riot_id)
    
    needs_commit = False
    
    if user.puuid != puuid:
        user.puuid = puuid
        needs_commit = True
        
    if user.solo_tier != solo_tier:
        user.solo_tier = solo_tier
        needs_commit = True

    if user.flex_tier != flex_tier:
        user.flex_tier = flex_tier
        needs_commit = True
        
    new_tier = solo_tier if solo_tier else flex_tier
        
    if user.tier != new_tier:
        user.tier = new_tier
        needs_commit = True
    
    if needs_commit:
        user.last_rank_update_at = datetime.now()
        db.session.commit()
    elif user.puuid:
        user.last_rank_update_at = datetime.now()
        db.session.commit()

def get_match_format(m: Match, tournament: Tournament):
    """매치 스테이지에 따른 Bo3/Bo5 포맷 정보 반환"""
    if m.stage == "LEAGUE":
        return 3, 2, "Bo3" # Total, Wins needed, Name
    if m.stage in ("PLAYOFF", "FINAL"):
        return 5, 3, "Bo5"
    return 1, 1, "Single Match"

def calculate_league_standings(tournament: Tournament) -> list:
    """리그전 순위 계산 (승점 -> 득실차)"""
    league_matches = Match.query.filter_by(tournament_id=tournament.id, stage="LEAGUE").all()
    teams = Team.query.filter_by(tournament_id=tournament.id).all()
    
    stats = {
        team.id: {
            'team_id': team.id, 'team_name': team.name,
            'P': 0, 'W': 0, 'L': 0, 'GD': 0, 'GF': 0, 'GA': 0
        } for team in teams
    }

    for m in league_matches:
        if m.team1_score is None or m.team2_score is None:
            continue
            
        s1, s2 = m.team1_score, m.team2_score
        if s1 < 2 and s2 < 2: 
             continue 
        
        t1, t2 = m.team1_id, m.team2_id
        stats[t1]['GF'] += s1; stats[t1]['GA'] += s2
        stats[t2]['GF'] += s2; stats[t2]['GA'] += s1
        
        if s1 > s2:
            stats[t1]['P'] += 3; stats[t1]['W'] += 1; stats[t2]['L'] += 1
        else:
            stats[t2]['P'] += 3; stats[t2]['W'] += 1; stats[t1]['L'] += 1

    for tid in stats:
        stats[tid]['GD'] = stats[tid]['GF'] - stats[tid]['GA']
            
    return sorted(stats.values(), key=lambda x: (x['P'], x['GD']), reverse=True)

def calculate_tournament_winner(tournament: Tournament) -> Team | None:
    """토너먼트 우승팀 계산"""
    if tournament.status != "FINISHED":
        return None

    if tournament.type in ("KNOCKOUT", "LEAGUE_FINAL"):
        final_match = Match.query.filter_by(tournament_id=tournament.id)\
            .filter((Match.stage == "FINAL") | (Match.stage == "PLAYOFF"))\
            .order_by(Match.round_no.desc(), Match.match_no.desc()).first()

        if final_match and final_match.winner_team_id:
            return final_match.winner_team
        
    elif tournament.type == "LEAGUE":
        standings = calculate_league_standings(tournament)
        if standings:
            return db.session.get(Team, standings[0]['team_id'])

    return None

def progress_tournament_if_needed(tournament: Tournament):
    """매치 결과에 따라 토너먼트 진행 (다음 라운드 생성/종료)"""
    if tournament.type == "KNOCKOUT":
        max_round = db.session.query(db.func.max(Match.round_no)).filter_by(
            tournament_id=tournament.id, stage="PLAYOFF"
        ).scalar()
        
        if not max_round: return

        current_round_matches = Match.query.filter_by(
            tournament_id=tournament.id, stage="PLAYOFF", round_no=max_round
        ).all()
        
        if current_round_matches and any(m.status != "DONE" for m in current_round_matches):
            return 

        winners = []
        for m in current_round_matches:
             if m.winner_team_id: winners.append(m.winner_team)
                
        if max_round == 1:
            all_team_ids = {t.id for t in Team.query.filter_by(tournament_id=tournament.id).all()}
            playing_ids = {m.team1_id for m in current_round_matches} | {m.team2_id for m in current_round_matches}
            bye_ids = all_team_ids - playing_ids
            winners.extend(Team.query.filter(Team.id.in_(bye_ids)).all())

        if len(winners) <= 1:
            tournament.status = "FINISHED"
            db.session.commit()
            return

        next_round = max_round + 1
        match_no = 1
        winners.sort(key=lambda t: t.id) 

        for i in range(0, len(winners), 2):
            if i + 1 >= len(winners): break 
            t1, t2 = winners[i], winners[i+1]
            m = Match(
                tournament_id=tournament.id, stage="PLAYOFF", round_no=next_round,
                match_no=match_no, team1_id=t1.id, team2_id=t2.id, status="SCHEDULED"
            )
            db.session.add(m)
            match_no += 1
        db.session.commit()

    if tournament.type in ("LEAGUE", "LEAGUE_FINAL") and tournament.status == "IN_PROGRESS":
        league_matches = Match.query.filter_by(tournament_id=tournament.id, stage="LEAGUE").all()
        
        def is_done(m):
            return m.status == "DONE" and m.team1_score is not None and \
                   m.team2_score is not None and m.team1_score != m.team2_score

        if all(is_done(m) for m in league_matches):
            if tournament.type == "LEAGUE_FINAL":
                final_match = Match.query.filter_by(tournament_id=tournament.id, stage="FINAL").first()
                if not final_match:
                    standings = calculate_league_standings(tournament)
                    if len(standings) >= 2:
                        final_match = Match(
                            tournament_id=tournament.id, stage="FINAL", round_no=1, match_no=1,
                            team1_id=standings[0]['team_id'], team2_id=standings[1]['team_id'],
                            status="SCHEDULED"
                        )
                        db.session.add(final_match)
                        db.session.commit()
                elif final_match.status == "DONE":
                    tournament.status = "FINISHED"
                    db.session.commit()
            elif tournament.type == "LEAGUE":
                tournament.status = "FINISHED"
                db.session.commit()

def generate_league_round_robin(tournament, teams):
    """리그전 매치 생성"""
    round_no, match_no = 1, 1
    for (t1, t2) in combinations(teams, 2):
        m = Match(
            tournament_id=tournament.id, stage="LEAGUE", round_no=round_no,
            match_no=match_no, team1_id=t1.id, team2_id=t2.id, status="SCHEDULED"
        )
        db.session.add(m)
        match_no += 1
        if match_no > 4: 
            round_no += 1
            match_no = 1

def generate_knockout_initial_round(tournament, teams):
    """토너먼트 초기 대진표 생성"""
    num_teams = len(teams)
    if num_teams < 2: return

    teams.sort(key=lambda t: t.id, reverse=True) 
    
    n = 1
    while n < num_teams: n *= 2
    
    num_byes = n - num_teams
    playing_teams = teams[num_byes:]
    
    half_playing = len(playing_teams) // 2
    match_pairs = []
    
    for i in range(half_playing):
        t1 = playing_teams[i]
        t2 = playing_teams[len(playing_teams) - 1 - i]
        match_pairs.append((t1, t2))
    
    round_no, match_no = 1, 1
    for t1, t2 in match_pairs:
        m = Match(
            tournament_id=tournament.id, stage="PLAYOFF", round_no=round_no,
            match_no=match_no, team1_id=t1.id, team2_id=t2.id, status="SCHEDULED"
        )
        db.session.add(m)
        match_no += 1

def tournament_history_data_loader(tournament):
    """히스토리 페이지용 데이터 로드"""
    matches = Match.query.filter_by(tournament_id=tournament.id).order_by(Match.stage, Match.round_no, Match.match_no).all()
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
            tm.base_score = base
            tm.weighted_score = w_score
            if team.captain_user_id == user.id:
                 captain_user = user 
        
        team.base_total = base_total
        team.weighted_total = weighted_total
        team.captain_user = captain_user
        
    winner_team = calculate_tournament_winner(tournament)
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
    """총 라운드 수 계산"""
    teams_count = Team.query.filter_by(tournament_id=tournament_id).count()
    if teams_count < 2: return 0
    return math.ceil(math.log2(teams_count))

def auto_generate_teams_logic(tournament):
    """팀 자동 생성 로직 (성공 여부, 메시지 반환)"""
    TeamMember.query.filter(TeamMember.team.has(tournament_id=tournament.id)).delete(synchronize_session=False)
    Team.query.filter_by(tournament_id=tournament.id).delete(synchronize_session=False)
    Match.query.filter_by(tournament_id=tournament.id).delete(synchronize_session=False)
    db.session.commit()

    approved = Participant.query.filter_by(tournament_id=tournament.id, status="APPROVED").all()
    if not approved:
        return False, "팀을 생성할 승인된 참가자가 없습니다."

    players = []
    for p in approved:
        u = p.user
        base_score = get_user_score(u)
        roles_weighted = []
        if u.primary_role in ROLES: roles_weighted.append((u.primary_role, 1.0))
        if u.secondary_role1 in ROLES and u.secondary_role1 != u.primary_role: roles_weighted.append((u.secondary_role1, 0.9))
        if u.secondary_role2 in ROLES and u.secondary_role2 not in [r for r, _ in roles_weighted]: roles_weighted.append((u.secondary_role2, 0.8))

        if not roles_weighted: continue

        players.append({
            "participant": p, "user": u, "base_score": base_score, "roles_weighted": roles_weighted,
        })

    if not players:
        return False, "유효한 포지션을 가진 선수가 없어 팀을 생성할 수 없습니다."

    total_players = len(players)
    role_candidates = {role: [] for role in ROLES}
    for pl in players:
        for role, weight in pl["roles_weighted"]:
            role_candidates[role].append({
                "participant": pl["participant"], "user": pl["user"],
                "base_score": pl["base_score"], "effective_score": pl["base_score"] * weight,
                "role": role,
            })

    max_by_count = total_players // 5
    max_by_roles = min(len(role_candidates[role]) for role in ROLES)
    team_count = min(max_by_count, max_by_roles)

    if team_count <= 0:
        return False, "최소한 한 팀(5인, 전 포지션)을 구성하기에 충분한 선수가 없습니다."

    teams_data = [{"name": f"Team {i+1}", "members": [], "total_score": 0} for i in range(team_count)]
    assigned_ids = set()

    for role in ROLES:
        candidates = sorted(role_candidates[role], key=lambda c: c["effective_score"], reverse=True)
        for cand in candidates:
            if cand["participant"].id in assigned_ids: continue
            
            possible_teams = [t for t in teams_data if not any(m["role"] == role for m in t["members"]) and len(t["members"]) < 5]
            if not possible_teams: continue
            
            possible_teams.sort(key=lambda t: t["total_score"])
            team = possible_teams[0]
            team["members"].append({"participant": cand["participant"], "role": role, "base_score": cand["base_score"]})
            team["total_score"] += cand["base_score"]
            assigned_ids.add(cand["participant"].id)
            if all(any(m["role"] == role for m in t["members"]) for t in teams_data): break

    remaining_players = sorted([pl for pl in players if pl["participant"].id not in assigned_ids], key=lambda pl: pl["base_score"], reverse=True)
    
    for pl in remaining_players:
        not_full_teams = sorted([t for t in teams_data if len(t["members"]) < 5], key=lambda t: t["total_score"])
        if not not_full_teams: break
        
        team = not_full_teams[0]
        main_role = next((r for r, _ in pl["roles_weighted"] if r in ROLES), "FLEX")
        team["members"].append({"participant": pl["participant"], "role": main_role, "base_score": pl["base_score"]})
        team["total_score"] += pl["base_score"]
        assigned_ids.add(pl["participant"].id)

    for tdata in teams_data:
        captain_id = max(tdata["members"], key=lambda m: m["base_score"])["participant"].user.id
        team = Team(name=tdata["name"], tournament_id=tournament.id, captain_user_id=captain_id)
        db.session.add(team)
        db.session.flush()
        
        for m in tdata["members"]:
            db.session.add(TeamMember(team_id=team.id, participant_id=m["participant"].id, assigned_role=m["role"]))

    db.session.commit()
    return True, "팀이 생성되었습니다."