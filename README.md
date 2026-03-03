# 🏆 League of Legends Tournament Manager

이 프로젝트는 League of Legends (LoL) 교내/커뮤니티 토너먼트를 효율적으로 운영하고 관리하기 위해 개발된 웹 애플리케이션입니다.

특히, 프로젝트 선정부터 요구사항 정의, 시스템 설계, 구현 및 테스트에 이르는 전체 SDLC(소프트웨어 개발 생명 주기) 과정에 LLM(Large Language Model)을 적극적으로 활용하여 개발 효율성을 극대화했습니다.

참가자 등록부터 팀 구성, 대진표 생성, 승패 기록까지 대회의 전 과정을 관리자가 웹상에서 손쉽게 처리할 수 있으며, Riot API를 연동하여 참가자의 실제 티어를 자동으로 검증하는 기능을 포함하고 있습니다.

## ✨ 주요 기능 (Key Features)

### 1. 사용자 (User)
* **회원가입 및 인증:** 학번/이름 기반 회원가입 및 관리자 승인 시스템.
* **Riot 계정 연동:** Riot ID(닉네임#태그)를 입력하면 Riot API를 통해 **솔로/자유 랭크 티어를 자동으로 조회**하여 저장합니다.
* **팀 관리:** 팀을 생성하거나 기존 팀에 가입 신청을 할 수 있습니다.
* **대시보드:** 나의 경기 일정, 소속 팀 정보, 대회 진행 상황을 한눈에 확인합니다.

### 2. 관리자 (Admin)
* **대회 생성:** 토너먼트(Knockout) 또는 리그(League) 방식의 대회를 생성하고 관리합니다.
* **대진표 관리:** 라운드별 매치 생성 및 경기 결과(스코어) 입력을 처리합니다.
* **사용자 관리:** 가입 승인/거절 및 참가자 정보를 수정할 수 있습니다.

### 3. 시스템 (System)
* **자동 점수 산정:** 티어 정보를 바탕으로 참가자의 실력 점수(Weighted Score)를 계산하여 팀 밸런싱에 활용합니다.
* **반응형 디자인:** 모바일과 데스크톱 환경 모두 지원합니다.

---

## 🛠 기술 스택 (Tech Stack)

* **Backend:** Python 3.12, Flask
* **Database:** SQLite (SQLAlchemy ORM)
* **Frontend:** HTML5, CSS3, Jinja2 Templates
* **External API:** Riot Games API

---

## 🚀 배포 및 실행 가이드 (Deployment)

이 프로젝트는 **Render**, **Heroku**, **PythonAnywhere** 등의 클라우드 플랫폼에서 실행할 수 있도록 구성되어 있습니다.

### 1. 로컬 환경에서 실행 (Local Development)

```bash
# 1. 저장소 클론
git clone [https://github.com/your-username/your-repo-name.git](https://github.com/your-username/your-repo-name.git)
cd your-repo-name

# 2. 가상환경 생성 및 활성화
python -m venv venv
source venv/bin/activate  # Mac/Linux
# venv\Scripts\activate   # Windows

# 3. 패키지 설치
pip install -r requirements.txt

# 4. 환경 변수 설정
export RIOT_API_KEY="your_riot_api_key"

# 5. 서버 실행
python app.py
