from flask import Flask, render_template, request, redirect, url_for, session, flash, abort
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

from config import Config
from models import db, User, Tournament, Participant, Team, TeamMember, Match
from services import (
    get_user_score, get_member_weighted_score, update_user_riot_ranks,
    get_match_format, progress_tournament_if_needed,
    calculate_tournament_winner, calculate_league_standings,
    generate_league_round_robin, generate_knockout_initial_round,
    tournament_history_data_loader, calculate_theoretical_rounds,
    calculate_estimated_score, get_tournament_or_404, auto_generate_teams_logic,
    ROLES
)


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)
    db.init_app(app)

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

# -------------------- Helper --------------------

def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return db.session.get(User, user_id)


def login_required(role=None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            user = current_user()
            active_role = session.get("role") 
            
            if not user or not active_role:
                return redirect(url_for("login_page"))
            
            if role and active_role != role:
                flash("권한이 거부되었습니다. 현재 권한은 " + active_role + "입니다.")
                return redirect(url_for("login_page"))
            
            return fn(*args, **kwargs)
        return wrapper
    return decorator


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
    # 로그인 페이지 렌더링
    return render_template("login.html", roles=None)


@app.route("/login", methods=["POST"])
def login():
    # 로그인 로직 처리 (1단계: 인증, 2단계: 권한 선택)
    username = request.form.get("username")
    password = request.form.get("password")
    selected_role = request.form.get("selected_role")

    user = User.query.filter_by(username=username).first()

    if selected_role:
        if not user:
            flash("권한 선택을 위한 사용자를 찾을 수 없습니다.")
            return redirect(url_for("login_page"))
        
        if user.approval_status != "APPROVED":
            if user.approval_status == "REJECTED":
                db.session.delete(user)
                db.session.commit()
                flash(f"로그인에 실패했습니다. 요청이 거부되어 삭제되었습니다.")
                return redirect(url_for("login_page"))
                
            flash(f"로그인에 실패했습니다. 상태: '{user.approval_status}'.")
            return redirect(url_for("login_page"))
            
        all_roles = [r.strip() for r in user.role.split(',') if r.strip()]
        if selected_role not in all_roles:
            flash("유효하지 않은 권한 선택입니다.")
            return redirect(url_for("login_page"))
        active_role = selected_role
        
    else:
        if not user or not check_password_hash(user.password_hash, password):
            flash("유효하지 않은 아이디 또는 비밀번호입니다.")
            return redirect(url_for("login_page"))

        if user.approval_status != "APPROVED":
            if user.approval_status == "REJECTED":
                db.session.delete(user)
                db.session.commit()
                flash(f"로그인에 실패했습니다. 요청이 거부되어 삭제되었습니다.")
                return redirect(url_for("login_page"))
            flash(f"로그인에 실패했습니다. 상태: '{user.approval_status}'.")
            return redirect(url_for("login_page"))
        
        all_roles = [r.strip() for r in user.role.split(',') if r.strip()]
        if len(all_roles) > 1:
            return render_template("login.html", username=username, roles=all_roles)
        else:
            active_role = all_roles[0] if all_roles else "USER"
            

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
    # 회원가입 처리
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

    user = User(
        username=username,
        password_hash=generate_password_hash(password),
        role=initial_role_selection, 
        approval_status="PENDING", 
    )
    db.session.add(user)
    db.session.commit()

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
    # 유저 대시보드
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

@app.route("/user/profile", methods=["GET", "POST"])
@login_required(role="USER")
def user_profile():
    # 유저 프로필 수정 및 Riot ID 연동
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
                return redirect(url_for("user_profile"))

        if riot_id:
            existing_riot = User.query.filter(User.summoner_riot_id == riot_id, User.id != user.id).first()
            if existing_riot:
                flash("이미 다른 사용자가 사용 중인 Riot ID입니다.")
                return redirect(url_for("user_profile"))

        user.student_id = student_id or None
        user.real_name = real_name or None
        user.summoner_riot_id = riot_id or None
        user.primary_role = primary_role or None
        user.secondary_role1 = secondary_role1 or None
        user.secondary_role2 = secondary_role2 or None
        
        # 랭크 정보 초기화 (update_user_riot_ranks가 다음 호출 시 처리)
        user.tier = None; user.solo_tier = None; user.flex_tier = None; user.puuid = None

        if riot_id:
            update_user_riot_ranks(user) # 즉시 업데이트 시도

        db.session.commit()
        flash("프로필이 업데이트되었습니다.")
        return redirect(url_for("user_profile"))

    return render_template("user/profile.html", user=user)


@app.route("/user/tournaments")
@login_required(role="USER")
def user_tournaments():
    # 토너먼트 목록 조회
    tournaments = Tournament.query.all()
    user = current_user()
    for t in tournaments:
        t.my_participant = Participant.query.filter_by(user_id=user.id, tournament_id=t.id).first()
    return render_template("user/tournaments.html", tournaments=tournaments)


@app.route("/user/tournaments/<int:tournament_id>/apply", methods=["GET", "POST"])
@login_required(role="USER")
def user_apply(tournament_id):
    # 토너먼트 참가 신청
    user = current_user()
    tournament = get_tournament_or_404(tournament_id)
    is_application_open = (tournament.status == "OPEN")
                
    if request.method == "POST":
        if tournament.status not in ("OPEN",):
            flash(f"신청이 마감되었습니다. 토너먼트 상태는 {tournament.status}입니다.")
            return redirect(url_for("user_apply", tournament_id=tournament_id))

        if not user.student_id or not user.real_name or not user.summoner_riot_id:
            flash("신청 전에 프로필 필수 정보를 입력해주세요.")
            return redirect(url_for("user_profile"))

        if not user.primary_role or not user.secondary_role1 or not user.secondary_role2:
            flash("신청 전에 포지션을 모두 설정해주세요.")
            return redirect(url_for("user_profile"))

        existing = Participant.query.filter_by(user_id=user.id, tournament_id=tournament.id).first()
        if existing:
            flash(f"이미 신청하셨습니다. 현재 상태: {existing.status}")
            return redirect(url_for("user_apply", tournament_id=tournament_id))

        p = Participant(user_id=user.id, tournament_id=tournament.id, status="PENDING")
        db.session.add(p)
        db.session.commit()
        flash("참가 신청이 완료되었습니다.")
        return redirect(url_for("user_apply", tournament_id=tournament_id))

    current_participant = Participant.query.filter_by(user_id=user.id, tournament_id=tournament_id).first()
    return render_template("user/apply.html", user=user, tournament=tournament, current_participant=current_participant, is_application_open=is_application_open)


@app.route("/user/tournaments/<int:tournament_id>/team")
@login_required(role="USER")
def user_team(tournament_id):
    # 내 팀 정보 조회
    user = current_user()
    tournament = get_tournament_or_404(tournament_id)
    participant = Participant.query.filter_by(user_id=user.id, tournament_id=tournament.id).first()
    team = participant.team_membership.team if (participant and participant.team_membership) else None

    if not team:
        return render_template("user/team.html", team=None, is_captain=False, tournament=tournament)

    is_captain = (team.captain_user_id == user.id)
    
    # 점수 계산하여 객체에 부착
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


@app.route("/user/tournaments/<int:tournament_id>/team/rename", methods=["POST"])
@login_required(role="USER")
def user_team_rename(tournament_id):
    # 팀 이름 변경 (주장 전용)
    user = current_user()
    tournament = get_tournament_or_404(tournament_id)
    participant = Participant.query.filter_by(user_id=user.id, tournament_id=tournament.id).first()

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


@app.route("/user/tournaments/<int:tournament_id>/matches")
@login_required(role="USER")
def user_matches(tournament_id):
    # 내 팀의 매치 목록
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


@app.route("/user/matches/<int:match_id>/report", methods=["GET", "POST"])
@login_required(role="USER")
def user_match_report(match_id):
    # 매치 결과 보고 (주장 전용)
    user = current_user()
    m = Match.query.get_or_404(match_id)
    tournament = get_tournament_or_404(m.tournament_id)

    my_participant = Participant.query.filter_by(user_id=user.id, tournament_id=tournament.id).first()
    if not my_participant or not my_participant.team_membership:
        flash("팀에 소속되어 있지 않습니다.")
        return redirect(url_for("user_matches", tournament_id=tournament.id))

    my_team = my_participant.team_membership.team
    if my_team.id not in (m.team1_id, m.team2_id):
        flash("참가자가 아닙니다.")
        return redirect(url_for("user_matches", tournament_id=tournament.id))

    if my_team.captain_user_id != user.id:
        flash("주장만 결과를 보고할 수 있습니다.")
        return redirect(url_for("user_matches", tournament_id=tournament.id))

    if request.method == "POST":
        try:
            s1 = int(request.form.get("team1_score", "0"))
            s2 = int(request.form.get("team2_score", "0"))
        except ValueError:
            flash("점수는 정수여야 합니다.")
            return redirect(url_for("user_match_report", match_id=match_id))
        
        bestof, wins_needed, format_name = get_match_format(m, tournament)

        if s1 < wins_needed and s2 < wins_needed:
            flash(f"{format_name} 조건 불충족: 최소 {wins_needed}승 필요.")
            return redirect(url_for("user_match_report", match_id=match_id))
        
        if s1 + s2 > bestof:
            flash(f"총 매치 수 {bestof} 초과.")
            return redirect(url_for("user_match_report", match_id=match_id))
        
        if s1 >= wins_needed and s2 >= wins_needed:
             flash("양 팀 모두 승리 조건을 달성할 수 없습니다.")
             return redirect(url_for("user_match_report", match_id=match_id))

        m.team1_score = s1; m.team2_score = s2; m.status = "DONE"
        m.winner_team_id = m.team1_id if s1 > s2 else (m.team2_id if s2 > s1 else None)
        db.session.commit()
        
        progress_tournament_if_needed(tournament)
        flash("결과가 제출되었습니다.")
        return redirect(url_for("user_matches", tournament_id=tournament.id))

    return render_template("user/match_report.html", match=m)


# -------------------- Team Routes ---------------------

@app.route("/team/<int:team_id>")
@login_required() 
def team_detail(team_id):
    # 팀 상세 정보 (공용)
    team = Team.query.get_or_404(team_id)
    base_total = 0; weighted_total = 0
    for tm in team.members:
        u = tm.participant.user
        base = get_user_score(u)
        w_score = get_member_weighted_score(u, tm.assigned_role)
        base_total += base; weighted_total += w_score
        tm.base_score = base; tm.weighted_score = w_score
    team.base_total = base_total; team.weighted_total = weighted_total
    return render_template("team/detail.html", team=team)


# -------------------- Admin Routes --------------------

@app.route("/admin/dashboard")
@login_required(role="ADMIN")
def admin_dashboard():
    # 관리자 대시보드
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


@app.route("/admin/tournaments", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_tournaments():
    # 토너먼트 생성 및 관리
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        t_type = request.form.get("type", "KNOCKOUT").strip()
        if not name: flash("대회 이름은 필수입니다.")
        elif t_type not in ("KNOCKOUT", "LEAGUE", "LEAGUE_FINAL"): flash("유효하지 않은 유형입니다.")
        else:
            db.session.add(Tournament(name=name, type=t_type, status="OPEN"))
            db.session.commit()
            flash(f"대회 '{name}' 생성 완료.")
            return redirect(url_for("admin_tournaments"))

    return render_template("admin/tournaments.html", tournaments=Tournament.query.all())


@app.route("/admin/tournaments/<int:tournament_id>/participants")
@login_required(role="ADMIN")
def admin_participants(tournament_id):
    # 참가자 관리
    tournament = get_tournament_or_404(tournament_id)
    participants = Participant.query.filter_by(tournament_id=tournament.id).all()
    return render_template("admin/participants.html", participants=participants, tournament=tournament)


@app.route("/admin/tournaments/<int:tournament_id>/participants/<int:participant_id>/approve", methods=["POST"])
@login_required(role="ADMIN")
def admin_approve_participant(tournament_id, participant_id):
    # 참가자 승인
    tournament = get_tournament_or_404(tournament_id)
    p = db.session.get(Participant, participant_id)
    if not p or p.tournament_id != tournament.id: abort(404)
    if p.status != "PENDING":
        flash("승인 대기 상태가 아닙니다.")
        return redirect(url_for("admin_participants", tournament_id=tournament_id))

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
            
    return redirect(url_for("admin_participants", tournament_id=tournament_id))


@app.route("/admin/tournaments/<int:tournament_id>/participants/<int:participant_id>/reject", methods=["POST"])
@login_required(role="ADMIN")
def admin_reject_participant(tournament_id, participant_id):
    # 참가자 거절
    tournament = get_tournament_or_404(tournament_id)
    p = db.session.get(Participant, participant_id)
    if p and p.tournament_id == tournament.id:
        p.status = "REJECTED"
        db.session.commit()
    return redirect(url_for("admin_participants", tournament_id=tournament_id))


@app.route("/admin/tournaments/<int:tournament_id>/participants/bulk-approve", methods=["POST"])
@login_required(role="ADMIN")
def admin_bulk_approve_participants(tournament_id):
    # 일괄 승인
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
    return redirect(url_for("admin_participants", tournament_id=tournament_id))


@app.route("/admin/tournaments/<int:tournament_id>/participants/<int:participant_id>/score", methods=["POST"])
@login_required(role="ADMIN")
def admin_update_approved_score(tournament_id, participant_id):
    # 승인된 참가자 점수 수정
    tournament = get_tournament_or_404(tournament_id)
    p = db.session.get(Participant, participant_id)
    if not p or p.tournament_id != tournament.id or p.status != "APPROVED": abort(404)
        
    try:
        p.user.actual_score = int(request.form.get("actual_score", ""))
        db.session.commit()
        flash("점수가 업데이트되었습니다.")
    except ValueError:
        flash("유효하지 않은 점수입니다.")

    return redirect(url_for("admin_participants", tournament_id=tournament_id))


@app.route("/admin/tournaments/<int:tournament_id>/teams", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_teams(tournament_id):
    # 팀 관리 및 수동 수정
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
        return redirect(url_for("admin_teams", tournament_id=tournament_id))

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


@app.route("/admin/tournaments/<int:tournament_id>/teams/auto-generate", methods=["POST"])
@login_required(role="ADMIN")
def admin_auto_generate_teams(tournament_id):
    # 팀 자동 생성 (로직은 services.py로 위임)
    tournament = get_tournament_or_404(tournament_id)
    success, msg = auto_generate_teams_logic(tournament)
    flash(msg)
    return redirect(url_for("admin_teams", tournament_id=tournament_id))


@app.route("/admin/permissions", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_permissions():
    # 관리자 권한 부여
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


@app.route("/admin/user_approvals")
@login_required(role="ADMIN")
def admin_user_approvals():
    # 회원 가입 승인 관리
    return render_template("admin/user_approvals.html", 
                           pending_users=User.query.filter_by(approval_status="PENDING").all(),
                           rejected_users=User.query.filter_by(approval_status="REJECTED").all())


@app.route("/admin/user_approvals/<int:user_id>/<action>", methods=["POST"])
@login_required(role="ADMIN")
def admin_handle_user_approval(user_id, action):
    # 회원 승인/거절/삭제 액션
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
    return redirect(url_for("admin_user_approvals"))


@app.route("/admin/tournaments/<int:tournament_id>", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_tournament(tournament_id):
    # 토너먼트 상세 및 진행 관리
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
        return redirect(url_for("admin_tournament", tournament_id=tournament_id))

    matches = Match.query.filter_by(tournament_id=tournament.id).order_by(Match.stage, Match.round_no, Match.match_no).all()
    total_rounds = calculate_theoretical_rounds(tournament_id)
    winner_team = calculate_tournament_winner(tournament)
    league_standings = calculate_league_standings(tournament) if tournament.type in ("LEAGUE", "LEAGUE_FINAL") else None

    return render_template("admin/tournament.html", 
        tournament=tournament, matches=matches, winner_team=winner_team,
        league_standings=league_standings, total_rounds=total_rounds
    )


@app.route("/admin/tournaments/<int:tournament_id>/generate_schedule", methods=["POST"])
@login_required(role="ADMIN")
def admin_generate_schedule(tournament_id):
    # 대진표 생성
    tournament = get_tournament_or_404(tournament_id)
    Match.query.filter_by(tournament_id=tournament.id).delete()
    db.session.commit()

    teams = Team.query.filter_by(tournament_id=tournament.id).all()
    if len(teams) < 2:
        flash("최소 2개 팀이 필요합니다.")
        return redirect(url_for("admin_tournament", tournament_id=tournament_id))

    if tournament.type == "KNOCKOUT":
        generate_knockout_initial_round(tournament, teams)
        tournament.current_stage = "PLAYOFF"
    else:
        generate_league_round_robin(tournament, teams)
        tournament.current_stage = "LEAGUE"

    tournament.status = "IN_PROGRESS"
    db.session.commit()
    flash("일정이 생성되었습니다.")
    return redirect(url_for("admin_tournament", tournament_id=tournament_id))


@app.route("/admin/matches/<int:match_id>/report", methods=["GET", "POST"])
@login_required(role="ADMIN")
def admin_match_report(match_id):
    # 관리자 경기 결과 수정
    m = Match.query.get_or_404(match_id)
    tournament = get_tournament_or_404(m.tournament_id)

    if request.method == "POST":
        try:
            s1 = int(request.form.get("team1_score", "0"))
            s2 = int(request.form.get("team2_score", "0"))
        except ValueError:
            flash("점수는 정수여야 합니다.")
            return redirect(url_for("admin_match_report", match_id=match_id))

        bestof, wins_needed, format_name = get_match_format(m, tournament)
        if s1 < wins_needed and s2 < wins_needed:
            flash(f"{format_name} 조건 불충족: {wins_needed}승 필요.")
            return redirect(url_for("admin_match_report", match_id=match_id))
        
        if s1 + s2 > bestof:
             flash(f"총 매치 수 {bestof} 초과.")
             return redirect(url_for("admin_match_report", match_id=match_id))

        if s1 >= wins_needed and s2 >= wins_needed:
             flash("양 팀 모두 승리 불가.")
             return redirect(url_for("admin_match_report", match_id=match_id))

        m.team1_score = s1; m.team2_score = s2; m.status = "DONE"
        m.winner_team_id = m.team1_id if s1 > s2 else (m.team2_id if s2 > s1 else None)
        db.session.commit()

        progress_tournament_if_needed(tournament)
        flash("결과 수정 완료.")
        return redirect(url_for("admin_tournament", tournament_id=tournament.id))

    return render_template("admin/match_report.html", match=m)


@app.route("/tournament/<int:tournament_id>/history")
@login_required()
def tournament_history(tournament_id):
    # 토너먼트 기록 (공개)
    tournament = get_tournament_or_404(tournament_id)
    data = tournament_history_data_loader(tournament)
    data['total_rounds'] = calculate_theoretical_rounds(tournament_id)
    return render_template("tournament_history.html", **data)


if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=True)