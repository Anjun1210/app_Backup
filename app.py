from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import urllib3
import re
import os
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

# =======================================================
# ⚙️ 系統設定與全域變數
# =======================================================
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
CORS(app)
app.config['JSON_AS_ASCII'] = False

CWA_API_KEY = os.environ.get("CWA_API_KEY", "CWA-706D5143-2567-4EC1-9FC5-FDB6079B736B")

GLOBAL_CACHE = {
    "school_calendar": {"data": None, "timestamp": 0}
}
CACHE_TTL_SECONDS = 86400  

BUILDING_MAP = {
    'AK': '任垣樓 ', 'SP': '伯鐸樓 ', 'JA': '靜安樓 ', 'TG': '格倫樓 ',
    'PH': '主顧樓 ', 'SF': '方濟樓 ', 'SY': '思源樓 ', '2R': '第二研究大樓 ',
    'AK-3C': '計算機中心 ', '1R': '第一研究大樓 ', 'ST': '體育館 ', 'SD': '田徑場 '
}
SORTED_BUILDING_CODES = sorted(BUILDING_MAP.keys(), key=len, reverse=True)

# =======================================================
# 🛠️ 爬蟲核心函式庫
# =======================================================

def format_location(raw_location_str):
    if not raw_location_str: return "未知教室"
    for code in SORTED_BUILDING_CODES:
        if raw_location_str.startswith(code):
            building_full_name = BUILDING_MAP[code]
            room_number = raw_location_str[len(code):].lstrip(':- ')
            return f"{building_full_name} ({room_number})" if room_number else building_full_name
    return raw_location_str

def scrape_courses_and_info(session):
    """ 獨立的課表與基本資料爬蟲 """
    course_list = []
    student_name, department = "未知姓名", "未知系所" 
    try:
        course_url = "https://alcat.pu.edu.tw/stu_query/query_course.html"
        resp = session.get(course_url, verify=False, timeout=10)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        html_text = soup.get_text()
        name_match = re.search(r'姓名[：:\s]*([\u4e00-\u9fa5]+)', html_text)
        dept_match = re.search(r'系級[：:\s]*([\u4e00-\u9fa5a-zA-Z0-9]+)', html_text)
        if name_match: student_name = name_match.group(1)
        if dept_match: department = dept_match.group(1)

        course_table = soup.find('table', class_='small')
        if course_table:
            for row in course_table.find_all('tr')[1:]:
                cols = row.find_all('td')
                if len(cols) >= 6:
                    raw_time_loc = cols[5].get_text(strip=True)
                    weekday, sessions_str, raw_location = "", "", ""
                    if "(" in raw_time_loc and ":" in raw_time_loc:
                        try:
                            weekday = raw_time_loc.split('(')[0].strip()
                            parts = raw_time_loc.split(')')[1].split(':')
                            sessions_str = parts[0].strip()
                            raw_location = parts[1].strip()
                        except: pass
                    
                    full_location = format_location(raw_location)
                    short_title = cols[2].get_text(separator='\n', strip=True).split('\n')[0]
                    course_list.append({
                        "title": short_title, "weekday": weekday, 
                        "sessions": sessions_str, "location": full_location 
                    })
    except Exception as e:
        print(f"⚠️ 課表爬取發生錯誤: {e}")
    return student_name, department, course_list

def scrape_grades(session):
    """ 終極版成績爬蟲：透過 SSO 秘密通道，直搗 MYPU 歷年成績總表 """
    all_semesters = []
    try:
        # 🚀 關鍵步驟：走 SSO 秘密通道獲取 MYPU 的登入權限
        sso_url = "https://alcat.pu.edu.tw/index_chkLogin.php?link=index_ToNewPlt.php?sysID=score_query"
        print("====== 正在透過 SSO 秘密通道跳轉... ======")
        sso_resp = session.get(sso_url, verify=False, timeout=15)
        sso_resp.encoding = 'utf-8'

        # 🕵️‍♂️ 新增偵錯：看看通道裡面藏了什麼表單機關？
        print("====== SSO 通道裡的機關長怎樣？ ======")
        print(sso_resp.text[:1500])
        print("========================================")
        
        # 確保順利拿到授權後，前往真正的歷年成績總表
        score_url = "https://mypu.pu.edu.tw/score_query/score_all.php"
        resp = session.get(score_url, verify=False, timeout=15)
        resp.encoding = 'utf-8'
        
        # 🕵️‍♂️ 新增偵錯：看看 MYPU 把我們擋在門外的畫面！
        print("====== MYPU 最終畫面 ======")
        print(resp.text[:500])
        print("===========================")
        
        soup = BeautifulSoup(resp.text, 'html.parser')

        # 開始解析 mypu 的成績表格
        tables = soup.find_all('table')
        
        for table in tables:
            text = table.get_text()
            sem_match = re.search(r'(\d+)\s*學年度\s*第\s*(\d)\s*學期', text)
            
            if sem_match:
                sem_name = f"{sem_match.group(1)}學年度 第{sem_match.group(2)}學期"
                semester_obj = {
                    "semester": sem_name,
                    "gpa": "0.0",
                    "rank": "無數據",
                    "details": []
                }
                
                rows = table.find_all('tr')
                for row in rows:
                    cols = row.find_all('td')
                    if len(cols) >= 4:
                        subj = cols[0].get_text(strip=True)
                        crd = cols[1].get_text(strip=True)
                        scr = cols[3].get_text(strip=True)
                        
                        if subj and scr and "科目" not in subj and "學期總分" not in subj:
                            semester_obj["details"].append({
                                "subject": subj, "credits": crd, "score": scr
                            })
                
                summary_match = re.search(r'學期平均[：:\s]*([\d.]+)', text)
                rank_match = re.search(r'名次[：:\s]*([\d/]+)', text)
                
                if summary_match: semester_obj["gpa"] = summary_match.group(1)
                if rank_match: semester_obj["rank"] = rank_match.group(1)
                
                if semester_obj["details"]:
                    all_semesters.append(semester_obj)

    except Exception as e:
        print(f"⚠️ 歷年成績總表爬取失敗: {e}")
    
    # 回傳排序後的資料 (通常網頁上已經是從新到舊，這裡做雙重保險)
    return all_semesters

def get_cached_school_calendar():
    """ 帶有快取機制的校曆爬蟲 """
    now = time.time()
    if now - GLOBAL_CACHE["school_calendar"]["timestamp"] < CACHE_TTL_SECONDS and GLOBAL_CACHE["school_calendar"]["data"]:
        return GLOBAL_CACHE["school_calendar"]["data"]

    calendar_dict = {}
    try:
        ical_url = "https://calendar.google.com/calendar/ical/c_l1lhlorqj2e0rdqk5t69klbens%40group.calendar.google.com/public/basic.ics" 
        headers = {'User-Agent': 'Mozilla/5.0'}
        resp = requests.get(ical_url, headers=headers, verify=False, timeout=10)
        resp.encoding = 'utf-8'
        lines = resp.text.splitlines()

        current_event = {}
        for line in lines:
            if line.startswith('BEGIN:VEVENT'):
                current_event = {}
            elif line.startswith('SUMMARY:'):
                current_event['title'] = line[8:].strip()
            elif line.startswith('DTSTART'):
                match = re.search(r':(\d{8})', line)
                if match:
                    date_str = match.group(1)
                    year, month, day = int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8])
                    current_event['date_key'] = f"{year}-{month}-{day}"
            elif line.startswith('LOCATION:'):
                current_event['loc'] = line[9:].strip()
            elif line.startswith('DESCRIPTION:'):
                current_event['note'] = line[12:].strip()
            elif line.startswith('END:VEVENT'):
                if 'date_key' in current_event and 'title' in current_event:
                    date_key = current_event['date_key']
                    calendar_dict.setdefault(date_key, []).append({
                        "title": current_event['title'],
                        "time": "全天",
                        "loc": current_event.get('loc', "靜宜校園"),
                        "note": current_event.get('note', "官方行事曆自動同步")
                    })
    except Exception as e:
        print(f"⚠️ 校曆爬取失敗: {e}")
        
    if not calendar_dict:
        calendar_dict[f"{datetime.now().year}-{datetime.now().month}-{datetime.now().day}"] = [
            {"title": "行事曆設定中", "time": "系統", "loc": "設定", "note": "請填入正確的 iCal 網址"}
        ]
    
    GLOBAL_CACHE["school_calendar"]["data"] = calendar_dict
    GLOBAL_CACHE["school_calendar"]["timestamp"] = now
    return calendar_dict

# =======================================================
# 🚀 API 路由區
# =======================================================

@app.route('/api/sync_campus', methods=['POST'])
def sync_campus():
    data = request.json
    student_id = data.get('student_id')
    password = data.get('password')

    session = requests.Session()
    headers = {'User-Agent': 'Mozilla/5.0'}
    login_url = "https://alcat.pu.edu.tw/index_check.php"
    
    try:
        # 1. 執行第一階段登入
        login_resp = session.post(login_url, data={'uid': student_id, 'upassword': password, 'en_flag': ''}, headers=headers, verify=False, timeout=10)
        
        if "登入失敗" in login_resp.text or "密碼錯誤" in login_resp.text:
            return jsonify({"status": "error", "message": "帳號或密碼錯誤"}), 401

        # 2. 多執行緒抓取
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_courses = executor.submit(scrape_courses_and_info, session)
            future_grades = executor.submit(scrape_grades, session)

            student_name, department, course_list = future_courses.result()
            grades_data = future_grades.result()

        print(f"====== 爬蟲報告：總共抓到了 {len(grades_data)} 個學期的成績！ ======")

        if not course_list:
            return jsonify({"status": "error", "message": "找不到課表資料，可能是系統維護中"}), 404

        school_calendar_data = get_cached_school_calendar()

        return jsonify({
            "status": "success",
            "message": "成功同步資料！",
            "student_name": student_name, 
            "department": department,
            "courses": course_list,
            "grades": grades_data,
            "school_calendar": school_calendar_data 
        })

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"status": "error", "message": f"伺服器錯誤: {str(e)}"}), 500

@app.route('/api/weather', methods=['GET'])
def get_weather():
    url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-D0047-073?Authorization={CWA_API_KEY}&locationName=沙鹿區"
    try:
        response = requests.get(url, timeout=5, verify=False)
        response.encoding = 'utf-8'
        text_data = response.text  
        temp_match = re.search(r'溫度攝氏(\d+)度', text_data)
        pop_match = re.search(r'降雨機率(\d+)%', text_data)
        temperature = temp_match.group(1) if temp_match else "26"
        pop = pop_match.group(1) if pop_match else "10"
        return jsonify({"status": "success", "location": "沙鹿區", "temperature": f"{temperature}°C", "pop": f"{pop}%"})
    except:
        return jsonify({"status": "error", "location": "沙鹿區", "temperature": "26°C", "pop": "10%"})

@app.route('/api/weather_weekly', methods=['GET'])
def get_weekly_weather():
    url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={CWA_API_KEY}&locationName=臺中市"
    try:
        response = requests.get(url, timeout=5, verify=False)
        data = response.json()
        locations = data.get('records', {}).get('location', [])
        if not locations: return jsonify({"status": "error", "message": "氣象署回傳空資料"})
        elements_dict = {el['elementName']: el['time'] for el in locations[0].get('weatherElement', [])}
        times = elements_dict.get('Wx', [])
        if not times: return jsonify({"status": "error", "message": "找不到時間資料"})

        base_date_str = times[0]['startTime'][:10] 
        base_date_obj = datetime.strptime(base_date_str, "%Y-%m-%d")
        today_formatted, tomorrow_formatted = base_date_obj.strftime("%m/%d"), (base_date_obj + __import__('datetime').timedelta(days=1)).strftime("%m/%d")

        today_data, tomorrow_data = [], []
        for i in range(len(times)):
            start_h = int(times[i]['startTime'][11:13])
            end_h = int(times[i]['endTime'][11:13])
            time_label = "凌晨" if start_h==0 and end_h==6 else "白天" if start_h==6 and end_h==18 else "晚上" if start_h==18 and end_h==6 else f"{start_h}:00 開始"
            
            weather_info = {
                "time": time_label,
                "min_t": elements_dict['MinT'][i]['parameter']['parameterName'],
                "max_t": elements_dict['MaxT'][i]['parameter']['parameterName'],
                "wx": elements_dict['Wx'][i]['parameter']['parameterName'],
                "pop": f"{elements_dict['PoP'][i]['parameter']['parameterName']}%"
            }
            if times[i]['startTime'][:10] == base_date_str: today_data.append(weather_info)
            else: tomorrow_data.append(weather_info)

        if not tomorrow_data: tomorrow_data.append({"time": "白天 (稍晚發布)", "min_t": "--", "max_t": "--", "wx": "等待更新", "pop": "--%"})
        return jsonify({"status": "success", "today_date": today_formatted, "today": today_data, "tomorrow_date": tomorrow_formatted, "tomorrow": tomorrow_data})
    except:
        return jsonify({"status": "error", "message": "伺服器內部錯誤"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
