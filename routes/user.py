from flask import Blueprint, render_template, request, redirect, url_for, flash
from models import db, User, Tournament, Participant, Match
from services import (
    get_user_score, get_member_weighted_score, update_user_riot_ranks,
    get_match_format, progress_tournament_if_needed, get_tournament_or_404
)
from utils import login_required, current_user

bp = Blueprint('user', __name__, url_prefix='/user')

@bp.route("/dashboard")
@login_required(role="USER")
def user_dashboard():
    """유저 대시보드"""
    user = current_user()
    update_user_riot_ranks(user)
    
    tournaments = Tournament.query.all()
    active_participants = Participant.query.filter_by(
        user_id=user.id, status="APPROVED"
    ).join(Tournament).filter(Tournament.status.in_(["OPEN", "IN_PROGRESS"])).all()
    
    return render_template(
        "user/dashboard.html",
        user=user,
        tournaments=tournaments, 
        has_active_tournaments=(len(active_participants) > 0), 
    )

@bp.route("/profile", methods=["GET", "POST"])
@login_required(role="USER")
def user_profile():
    """유저 프로필 수정 및 Riot ID 연동"""
    user = current_user()

    if request.method == "POST":
        student_id = request.form.get("student_id", "").strip()
        real_name = request.form.get("real_name", "").strip()
        riot_id = request.form.get("riot_id", "").strip()
        primary_role = request.form.get("primary_role", "").strip()
        secondary_role1 = request.form.get("secondary_role1", "").strip()
        secondary_role2 = request.form.get("secondary_role2", "").strip()

        if student_id:
            existing = User.query.filter(User.student_id == student_id, User.id != user.id).first()
            if existing:
                flash("이미 다른 사용자가 사용 중인 학번입니다.")
                return redirect(url_for("user.user_profile"))

        if riot_id:
            existing_riot = User.query.filter(User.summoner_riot_id == riot_id, User.id != user.id).first()
            if existing_riot:
                flash("이미 다른 사용자가 사용 중인 Riot ID입니다.")
                return redirect(url_for("user.user_profile"))

        user.student_id = student_id or None
        user.real_name = real_name or None
        user.summoner_riot_id = riot_id or None
        user.primary_role = primary_role or None
        user.secondary_role1 = secondary_role1 or None
        user.secondary_role2 = secondary_role2 or None
        
        # 랭크 정보 초기화
        user.tier = None; user.solo_tier = None; user.flex_tier = None; user.puuid = None

        if riot_id:
            update_user_riot_ranks(user, force_update=True)

        db.session.commit()
        flash("프로필이 업데이트되었습니다.")
        return redirect(url_for("user.user_profile"))

    return render_template("user/profile.html", user=user)


@bp.route("/tournaments")
@login_required(role="USER")
def user_tournaments():
    """토너먼트 목록 조회"""
    tournaments = Tournament.query.all()
    user = current_user()
    for t in tournaments:
        t.my_participant = Participant.query.filter_by(user_id=user.id, tournament_id=t.id).first()
    return render_template("user/tournaments.html", tournaments=tournaments)


@bp.route("/tournaments/<int:tournament_id>/apply", methods=["GET", "POST"])
@login_required(role="USER")
def user_apply(tournament_id):
    """토너먼트 참가 신청"""
    user = current_user()
    tournament = get_tournament_or_404(tournament_id)
    is_application_open = (tournament.status == "OPEN")
                
    if request.method == "POST":
        if tournament.status not in ("OPEN",):
            flash(f"신청이 마감되었습니다. 토너먼트 상태는 {tournament.status}입니다.")
            return redirect(url_for("user.user_apply", tournament_id=tournament_id))

        if not user.student_id or not user.real_name or not user.summoner_riot_id:
            flash("신청 전에 프로필 필수 정보를 입력해주세요.")
            return redirect(url_for("user.user_profile"))

        if not user.primary_role or not user.secondary_role1 or not user.secondary_role2:
            flash("신청 전에 포지션을 모두 설정해주세요.")
            return redirect(url_for("user.user_profile"))

        existing = Participant.query.filter_by(user_id=user.id, tournament_id=tournament.id).first()
        if existing:
            flash(f"이미 신청하셨습니다. 현재 상태: {existing.status}")
            return redirect(url_for("user.user_apply", tournament_id=tournament_id))

        p = Participant(user_id=user.id, tournament_id=tournament.id, status="PENDING")
        db.session.add(p)
        db.session.commit()
        flash("참가 신청이 완료되었습니다.")
        return redirect(url_for("user.user_apply", tournament_id=tournament_id))

    current_participant = Participant.query.filter_by(user_id=user.id, tournament_id=tournament_id).first()
    return render_template("user/apply.html", user=user, tournament=tournament, current_participant=current_participant, is_application_open=is_application_open)


@bp.route("/tournaments/<int:tournament_id>/team")
@login_required(role="USER")
def user_team(tournament_id):
    """내 팀 정보 조회"""
    user = current_user()
    tournament = get_tournament_or_404(tournament_id)
    participant = Participant.query.filter_by(user_id=user.id, tournament_id=tournament.id).first()
    team = participant.team_membership.team if (participant and participant.team_membership) else None

    if not team:
        return render_template("user/team.html", team=None, is_captain=False, tournament=tournament)

    is_captain = (team.captain_user_id == user.id)
    
    # 점수 계산
    base_total = 0
    weighted_total = 0
    for tm in team.members:
        u = tm.participant.user
        base = get_user_score(u)
        w_score = get_member_weighted_score(u, tm.assigned_role)
        base_total += base; weighted_total += w_score
        tm.base_score = base; tm.weighted_score = w_score
    team.base_total = base_total; team.weighted_total = weighted_total

    return render_template("user/team.html", team=team, is_captain=is_captain, tournament=tournament)


@bp.route("/tournaments/<int:tournament_id>/team/rename", methods=["POST"])
@login_required(role="USER")
def user_team_rename(tournament_id):
    """팀 이름 변경 (주장 전용)"""
    user = current_user()
    tournament = get_tournament_or_404(tournament_id)
    participant = Participant.query.filter_by(user_id=user.id, tournament_id=tournament.id).first()

    if not participant or not participant.team_membership:
        flash("팀에 배정되어 있지 않습니다.")
        return redirect(url_for("user.user_team", tournament_id=tournament_id))

    team = participant.team_membership.team
    if team.captain_user_id != user.id:
        flash("팀 주장만 팀 이름을 변경할 수 있습니다.")
        return redirect(url_for("user.user_team", tournament_id=tournament_id))

    new_name = request.form.get("team_name", "").strip()
    if not new_name:
        flash("팀 이름은 비워둘 수 없습니다.")
        return redirect(url_for("user.user_team", tournament_id=tournament_id))

    team.name = new_name
    db.session.commit()
    flash("팀 이름이 업데이트되었습니다.")
    return redirect(url_for("user.user_team", tournament_id=tournament_id))


@bp.route("/tournaments/<int:tournament_id>/matches")
@login_required(role="USER")
def user_matches(tournament_id):
    """내 팀의 매치 목록 조회"""
    user = current_user()
    tournament = get_tournament_or_404(tournament_id)
    my_participant = Participant.query.filter_by(user_id=user.id, tournament_id=tournament.id).first()
    my_team_id = my_participant.team_membership.team_id if (my_participant and my_participant.team_membership) else None

    if not my_team_id:
        return render_template("user/matches.html", matches=[], tournament=tournament, my_participant=my_participant, my_team_id=None)

    matches = Match.query.filter(
        Match.tournament_id == tournament.id,
        ((Match.team1_id == my_team_id) | (Match.team2_id == my_team_id))
    ).order_by(Match.stage, Match.round_no, Match.match_no).all()

    return render_template("user/matches.html", matches=matches, tournament=tournament, my_participant=my_participant, my_team_id=my_team_id)


@bp.route("/matches/<int:match_id>/report", methods=["GET", "POST"])
@login_required(role="USER")
def user_match_report(match_id):
    """매치 결과 보고 (주장 전용)"""
    user = current_user()
    m = Match.query.get_or_404(match_id)
    tournament = get_tournament_or_404(m.tournament_id)

    my_participant = Participant.query.filter_by(user_id=user.id, tournament_id=tournament.id).first()
    if not my_participant or not my_participant.team_membership:
        flash("팀에 소속되어 있지 않습니다.")
        return redirect(url_for("user.user_matches", tournament_id=tournament.id))

    my_team = my_participant.team_membership.team
    if my_team.id not in (m.team1_id, m.team2_id):
        flash("참가자가 아닙니다.")
        return redirect(url_for("user.user_matches", tournament_id=tournament.id))

    if my_team.captain_user_id != user.id:
        flash("주장만 결과를 보고할 수 있습니다.")
        return redirect(url_for("user.user_matches", tournament_id=tournament.id))

    if request.method == "POST":
        try:
            s1 = int(request.form.get("team1_score", "0"))
            s2 = int(request.form.get("team2_score", "0"))
        except ValueError:
            flash("점수는 정수여야 합니다.")
            return redirect(url_for("user.user_match_report", match_id=match_id))
        
        bestof, wins_needed, format_name = get_match_format(m, tournament)

        if s1 < wins_needed and s2 < wins_needed:
            flash(f"{format_name} 조건 불충족: 최소 {wins_needed}승 필요.")
            return redirect(url_for("user.user_match_report", match_id=match_id))
        
        if s1 + s2 > bestof:
            flash(f"총 매치 수 {bestof} 초과.")
            return redirect(url_for("user.user_match_report", match_id=match_id))
        
        if s1 >= wins_needed and s2 >= wins_needed:
             flash("양 팀 모두 승리 조건을 달성할 수 없습니다.")
             return redirect(url_for("user.user_match_report", match_id=match_id))

        m.team1_score = s1; m.team2_score = s2; m.status = "DONE"
        m.winner_team_id = m.team1_id if s1 > s2 else (m.team2_id if s2 > s1 else None)
        db.session.commit()
        
        progress_tournament_if_needed(tournament)
        flash("결과가 제출되었습니다.")
        return redirect(url_for("user.user_matches", tournament_id=tournament.id))

    return render_template("user/match_report.html", match=m)