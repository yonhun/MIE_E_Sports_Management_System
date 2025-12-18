from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from models import db, User, Tournament, Participant, Team, Match
from services import (
    get_user_score, get_member_weighted_score, update_user_riot_ranks,
    get_match_format, progress_tournament_if_needed, calculate_tournament_winner,
    calculate_league_standings, calculate_theoretical_rounds, calculate_estimated_score,
    get_tournament_or_404, auto_generate_teams_logic, generate_knockout_initial_round,
    generate_league_round_robin
)
from utils import login_required

bp = Blueprint('admin', __name__, url_prefix='/admin')

@bp.route("/dashboard")
@login_required(role="ADMIN")
def admin_dashboard():
    """관리자 대시보드"""
    all_users_with_riot_id = User.query.filter(User.summoner_riot_id.isnot(None)).all()
    for u in all_users_with_riot_id:
        update_user_riot_ranks(u)

    total_users_count = User.query.count()
    pending_users_count = User.query.filter_by(approval_status="PENDING").count()

    tournaments = Tournament.query.all()
    for t in tournaments:
        t.total_applications = Participant.query.filter_by(tournament_id=t.id).count()
        t.approved = Participant.query.filter_by(tournament_id=t.id, status="APPROVED").count()
        t.total_teams_count = Team.query.filter_by(tournament_id=t.id).count()
        t.pending_matches_count = Match.query.filter_by(tournament_id=t.id, status="SCHEDULED").count()
    
    return render_template("admin/dashboard.html", tournaments=tournaments, total_users_count=total_users_count, pending_users_count=pending_users_count)


@bp.route("/tournaments", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_tournaments():
    """토너먼트 생성 및 관리"""
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        t_type = request.form.get("type", "KNOCKOUT").strip()
        if not name: flash("대회 이름은 필수입니다.")
        elif t_type not in ("KNOCKOUT", "LEAGUE", "LEAGUE_FINAL"): flash("유효하지 않은 유형입니다.")
        else:
            db.session.add(Tournament(name=name, type=t_type, status="OPEN"))
            db.session.commit()
            flash(f"대회 '{name}' 생성 완료.")
            return redirect(url_for("admin.admin_tournaments"))

    return render_template("admin/tournaments.html", tournaments=Tournament.query.all())


@bp.route("/tournaments/<int:tournament_id>/participants")
@login_required(role="ADMIN")
def admin_participants(tournament_id):
    """참가자 관리 목록"""
    tournament = get_tournament_or_404(tournament_id)
    participants = Participant.query.filter_by(tournament_id=tournament.id).all()
    return render_template("admin/participants.html", participants=participants, tournament=tournament)


@bp.route("/tournaments/<int:tournament_id>/participants/<int:participant_id>/approve", methods=["POST"])
@login_required(role="ADMIN")
def admin_approve_participant(tournament_id, participant_id):
    """개별 참가자 승인 처리"""
    tournament = get_tournament_or_404(tournament_id)
    p = db.session.get(Participant, participant_id)
    if not p or p.tournament_id != tournament.id: abort(404)
    if p.status != "PENDING":
        flash("승인 대기 상태가 아닙니다.")
        return redirect(url_for("admin.admin_participants", tournament_id=tournament_id))

    input_score_str = request.form.get(f"input_score_{participant_id}", "").strip()
    if input_score_str:
        try:
            p.user.actual_score = int(input_score_str)
            p.status = "APPROVED"
            db.session.commit()
            flash("입력된 점수로 승인되었습니다.")
        except ValueError:
            flash("유효하지 않은 점수입니다.")
    else:
        est = calculate_estimated_score(p.user)
        if est is not None:
            p.user.actual_score = est
            p.status = "APPROVED"
            db.session.commit()
            flash("예상 점수로 승인되었습니다.")
        else:
            flash("점수 정보가 없어 승인할 수 없습니다.")
            
    return redirect(url_for("admin.admin_participants", tournament_id=tournament_id))


@bp.route("/tournaments/<int:tournament_id>/participants/<int:participant_id>/reject", methods=["POST"])
@login_required(role="ADMIN")
def admin_reject_participant(tournament_id, participant_id):
    """참가자 거절 처리"""
    tournament = get_tournament_or_404(tournament_id)
    p = db.session.get(Participant, participant_id)
    if p and p.tournament_id == tournament.id:
        p.status = "REJECTED"
        db.session.commit()
    return redirect(url_for("admin.admin_participants", tournament_id=tournament_id))


@bp.route("/tournaments/<int:tournament_id>/participants/bulk-approve", methods=["POST"])
@login_required(role="ADMIN")
def admin_bulk_approve_participants(tournament_id):
    """참가자 일괄 승인"""
    tournament = get_tournament_or_404(tournament_id)
    id_list = request.form.getlist("participant_id")
    approved_count = 0

    for pid in id_list:
        p = db.session.get(Participant, int(pid))
        if not p or p.status != "PENDING" or p.tournament_id != tournament.id: continue

        input_score = request.form.get(f"input_score_{p.id}", "").strip()
        if input_score:
            try:
                p.user.actual_score = int(input_score)
                p.status = "APPROVED"
                approved_count += 1
                continue
            except ValueError: pass

        est = calculate_estimated_score(p.user)
        if est is not None:
            p.user.actual_score = est
            p.status = "APPROVED"
            approved_count += 1

    db.session.commit()
    flash(f"{approved_count}명 승인 완료.")
    return redirect(url_for("admin.admin_participants", tournament_id=tournament_id))


@bp.route("/tournaments/<int:tournament_id>/participants/<int:participant_id>/score", methods=["POST"])
@login_required(role="ADMIN")
def admin_update_approved_score(tournament_id, participant_id):
    """승인된 참가자의 점수 수정"""
    tournament = get_tournament_or_404(tournament_id)
    p = db.session.get(Participant, participant_id)
    if not p or p.tournament_id != tournament.id or p.status != "APPROVED": abort(404)
        
    try:
        p.user.actual_score = int(request.form.get("actual_score", ""))
        db.session.commit()
        flash("점수가 업데이트되었습니다.")
    except ValueError:
        flash("유효하지 않은 점수입니다.")

    return redirect(url_for("admin.admin_participants", tournament_id=tournament_id))


@bp.route("/tournaments/<int:tournament_id>/teams", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_teams(tournament_id):
    """팀 관리 및 수동 수정"""
    tournament = get_tournament_or_404(tournament_id)
    teams = Team.query.filter_by(tournament_id=tournament.id).all()

    if request.method == "POST":
        for team in teams:
            new_name = request.form.get(f"team_name_{team.id}", "").strip()
            if new_name: team.name = new_name
            
            captain_str = request.form.get(f"captain_user_{team.id}", "").strip()
            if captain_str:
                cid = int(captain_str)
                if any(tm.participant.user.id == cid for tm in team.members):
                    team.captain_user_id = cid
        db.session.commit()
        flash("팀 정보가 업데이트되었습니다.")
        return redirect(url_for("admin.admin_teams", tournament_id=tournament_id))

    # 화면 표시용 점수 계산
    for team in teams:
        base_total = 0; weighted_total = 0
        for tm in team.members:
            u = tm.participant.user
            base = get_user_score(u)
            w = get_member_weighted_score(u, tm.assigned_role)
            base_total += base; weighted_total += w
            tm.base_score = base; tm.weighted_score = w
        team.base_total = base_total; team.weighted_total = weighted_total

    return render_template("admin/teams.html", teams=teams, tournament=tournament)


@bp.route("/tournaments/<int:tournament_id>/teams/auto-generate", methods=["POST"])
@login_required(role="ADMIN")
def admin_auto_generate_teams(tournament_id):
    """팀 자동 생성 요청"""
    tournament = get_tournament_or_404(tournament_id)
    success, msg = auto_generate_teams_logic(tournament)
    flash(msg)
    return redirect(url_for("admin.admin_teams", tournament_id=tournament_id))


@bp.route("/permissions", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_permissions():
    """관리자 권한 부여"""
    users = User.query.all()
    if request.method == "POST":
        target = User.query.get(request.form.get("user_id"))
        if target:
            roles = set([r.strip() for r in target.role.split(',') if r.strip()])
            roles.add("ADMIN")
            target.role = ",".join(sorted(list(roles)))
            db.session.commit()
            flash(f"{target.username}에게 관리자 권한 부여됨.")
    return render_template("admin/permissions.html", users=users)


@bp.route("/user_approvals")
@login_required(role="ADMIN")
def admin_user_approvals():
    """회원 가입 승인 관리"""
    return render_template("admin/user_approvals.html", 
                           pending_users=User.query.filter_by(approval_status="PENDING").all(),
                           rejected_users=User.query.filter_by(approval_status="REJECTED").all())


@bp.route("/user_approvals/<int:user_id>/<action>", methods=["POST"])
@login_required(role="ADMIN")
def admin_handle_user_approval(user_id, action):
    """회원 승인/거절/삭제 액션 처리"""
    user = User.query.get_or_404(user_id)
    if action == "approve":
        user.approval_status = "APPROVED"
        flash(f"{user.username} 승인됨.")
    elif action == "reject":
        user.approval_status = "REJECTED"
        flash(f"{user.username} 거부됨.")
    elif action == "delete":
        db.session.delete(user)
        flash(f"{user.username} 삭제됨.")
    db.session.commit()
    return redirect(url_for("admin.admin_user_approvals"))


@bp.route("/tournaments/<int:tournament_id>", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_tournament(tournament_id):
    """토너먼트 상세 및 진행 상태 관리"""
    tournament = get_tournament_or_404(tournament_id)

    if request.method == "POST":
        t_type = request.form.get("type", "").strip()
        if t_type and tournament.status == "OPEN":
            tournament.type = t_type
        
        status = request.form.get("status", "").strip()
        if status in ("OPEN", "IN_PROGRESS", "FINISHED"):
            tournament.status = status
            
        db.session.commit()
        flash("대회 설정 업데이트 완료.")
        return redirect(url_for("admin.admin_tournament", tournament_id=tournament_id))

    matches = Match.query.filter_by(tournament_id=tournament.id).order_by(Match.stage, Match.round_no, Match.match_no).all()
    total_rounds = calculate_theoretical_rounds(tournament_id)
    winner_team = calculate_tournament_winner(tournament)
    league_standings = calculate_league_standings(tournament) if tournament.type in ("LEAGUE", "LEAGUE_FINAL") else None

    return render_template("admin/tournament.html", 
        tournament=tournament, matches=matches, winner_team=winner_team,
        league_standings=league_standings, total_rounds=total_rounds
    )


@bp.route("/tournaments/<int:tournament_id>/generate_schedule", methods=["POST"])
@login_required(role="ADMIN")
def admin_generate_schedule(tournament_id):
    """대진표 생성"""
    tournament = get_tournament_or_404(tournament_id)
    Match.query.filter_by(tournament_id=tournament.id).delete()
    db.session.commit()

    teams = Team.query.filter_by(tournament_id=tournament.id).all()
    if len(teams) < 2:
        flash("최소 2개 팀이 필요합니다.")
        return redirect(url_for("admin.admin_tournament", tournament_id=tournament_id))

    if tournament.type == "KNOCKOUT":
        generate_knockout_initial_round(tournament, teams)
        tournament.current_stage = "PLAYOFF"
    else:
        generate_league_round_robin(tournament, teams)
        tournament.current_stage = "LEAGUE"

    tournament.status = "IN_PROGRESS"
    db.session.commit()
    flash("일정이 생성되었습니다.")
    return redirect(url_for("admin.admin_tournament", tournament_id=tournament_id))


@bp.route("/matches/<int:match_id>/report", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_match_report(match_id):
    """관리자 권한으로 경기 결과 입력 및 수정"""
    m = Match.query.get_or_404(match_id)
    tournament = get_tournament_or_404(m.tournament_id)

    if request.method == "POST":
        try:
            s1 = int(request.form.get("team1_score", "0"))
            s2 = int(request.form.get("team2_score", "0"))
        except ValueError:
            flash("점수는 정수여야 합니다.")
            return redirect(url_for("admin.admin_match_report", match_id=match_id))

        bestof, wins_needed, format_name = get_match_format(m, tournament)
        if s1 < wins_needed and s2 < wins_needed:
            flash(f"{format_name} 조건 불충족: {wins_needed}승 필요.")
            return redirect(url_for("admin.admin_match_report", match_id=match_id))
        
        if s1 + s2 > bestof:
             flash(f"총 매치 수 {bestof} 초과.")
             return redirect(url_for("admin.admin_match_report", match_id=match_id))

        if s1 >= wins_needed and s2 >= wins_needed:
             flash("양 팀 모두 승리 불가.")
             return redirect(url_for("admin.admin_match_report", match_id=match_id))

        m.team1_score = s1; m.team2_score = s2; m.status = "DONE"
        m.winner_team_id = m.team1_id if s1 > s2 else (m.team2_id if s2 > s1 else None)
        db.session.commit()

        progress_tournament_if_needed(tournament)
        flash("결과 수정 완료.")
        return redirect(url_for("admin.admin_tournament", tournament_id=tournament.id))

    return render_template("admin/match_report.html", match=m)