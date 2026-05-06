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

# 🔐 API 金鑰改用環境變數，若無則使用預設值
CWA_API_KEY = os.environ.get("CWA_API_KEY", "CWA-706D5143-2567-4EC1-9FC5-FDB6079B736B")

# 🧠 全域快取 (Caching)：避免頻繁爬取靜態網頁
GLOBAL_CACHE = {
    "school_calendar": {"data": None, "timestamp": 0}
}
CACHE_TTL_SECONDS = 86400  # 快取存活時間：24小時

BUILDING_MAP = {
    'AK': '任垣樓 ', 'SP': '伯鐸樓 ', 'JA': '靜安樓 ', 'TG': '格倫樓 ',
    'PH': '主顧樓 ', 'SF': '方濟樓 ', 'SY': '思源樓 ', '2R': '第二研究大樓 ',
    'AK-3C': '計算機中心 ', '1R': '第一研究大樓 ', 'ST': '體育館 ', 'SD': '田徑場 '
}
SORTED_BUILDING_CODES = sorted(BUILDING_MAP.keys(), key=len, reverse=True)

# =======================================================
# 🛠️ 爬蟲核心函式庫 (模組化設計)
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
    student_name, department = "蕭安均", "靜宜大學 資訊管理系" # 預設值
    try:
        course_url = "https://alcat.pu.edu.tw/stu_query/query_course.html"
        resp = session.get(course_url, verify=False, timeout=10)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # 1. 抓取基本資料
        html_text = soup.get_text()
        name_match = re.search(r'姓名[：:\s]*([\u4e00-\u9fa5]+)', html_text)
        dept_match = re.search(r'系級[：:\s]*([\u4e00-\u9fa5a-zA-Z0-9]+)', html_text)
        if name_match: student_name = name_match.group(1)
        if dept_match: department = dept_match.group(1)

        # 2. 抓取課表
        course_table = soup.find('table', class_='small')
        if course_table:
            for row in course_table.find_all('tr')[1:]:
                cols = row.find_all('td')
                if len(cols) >= 6:
                    raw_time_loc = cols[5].get_text(strip=True)
                    weekday, sessions, raw_location = "", "", ""
                    if "(" in raw_time_loc and ":" in raw_time_loc:
                        try:
                            weekday = raw_time_loc.split('(')[0].strip()
                            parts = raw_time_loc.split(')')[1].split(':')
                            sessions = parts[0].strip()
                            raw_location = parts[1].strip()
                        except: pass
                    
                    full_location = format_location(raw_location)
                    short_title = cols[2].get_text(separator='\n', strip=True).split('\n')[0]
                    course_list.append({
                        "title": short_title, "weekday": weekday, 
                        "sessions": sessions, "location": full_location 
                    })
    except Exception as e:
        print(f"⚠️ 課表爬取發生錯誤: {e}")
    return student_name, department, course_list

def scrape_grades(session):
    """ 升級版成績爬蟲：智慧尋找表頭防呆 """
    grades_list = []
    try:
        score_url = "https://alcat.pu.edu.tw/stu_query/query_score.html"
        resp = session.get(score_url, verify=False, timeout=10)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')

        # 智慧定位：尋找包含「科目」或「成績」字眼的表格
        for table in soup.find_all('table'):
            headers = [th.get_text(strip=True) for th in table.find_all(['th', 'td'])]
            header_str = "".join(headers)
            
            if "科目" in header_str or "成績" in header_str:
                for row in table.find_all('tr')[1:]:
                    cols = row.find_all('td')
                    if len(cols) >= 4: # 保守抓取前幾個欄位
                        subject = cols[0].get_text(strip=True)
                        credits = cols[1].get_text(strip=True)
                        score = cols[3].get_text(strip=True)
                        # 排除標題列或空資料
                        if subject and score and subject != "科目名稱":
                            grades_list.append({"subject": subject, "credits": credits, "score": score})
                break # 抓到成績表就跳出
    except Exception as e:
        print(f"⚠️ 成績爬取失敗: {e}")
    return grades_list

def get_cached_school_calendar():
    """ 帶有快取機制的校曆爬蟲 """
    now = time.time()
    # 如果快取有效，直接回傳記憶體內的資料
    if now - GLOBAL_CACHE["school_calendar"]["timestamp"] < CACHE_TTL_SECONDS and GLOBAL_CACHE["school_calendar"]["data"]:
        print("⚡ 使用快取的校園行事曆")
        return GLOBAL_CACHE["school_calendar"]["data"]

    print("🌐 重新爬取校園行事曆...")
    calendar_dict = {}
    try:
        cal_url = "https://www.pu.edu.tw/p/412-1000-1454.php" 
        resp = requests.get(cal_url, verify=False, timeout=10)
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'html.parser')

        current_year = datetime.now().year
        for row in soup.find_all('tr'):
            text = row.get_text(separator=' ', strip=True)
            date_match = re.search(r'(1[0-2]|[1-9])[月/]([1-3]?[0-9])日?', text)
            
            if date_match:
                month, day = date_match.group(1), date_match.group(2)
                date_key = f"{current_year}-{month}-{day}"
                event_title = re.sub(r'(1[0-2]|[1-9])[月/]([1-3]?[0-9])日?', '', text).strip()
                event_title = re.sub(r'[^a-zA-Z0-9\u4e00-\u9fa5]', '', event_title) 
                
                if len(event_title) >= 2:
                    calendar_dict.setdefault(date_key, []).append({
                        "title": event_title[:15], "time": "全天",
                        "loc": "靜宜校園", "note": "來自學校官網自動同步"
                    })
    except Exception as e:
        print(f"⚠️ 校曆爬取失敗: {e}")
        
    if not calendar_dict:
        calendar_dict[f"{datetime.now().year}-{datetime.now().month}-{datetime.now().day}"] = [
            {"title": "校曆同步檢查中", "time": "系統", "loc": "API", "note": "請檢查學校官網是否改版"}
        ]
    
    # 寫入快取
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
        # 1. 執行登入 (必須先同步完成)
        login_resp = session.post(login_url, data={'uid': student_id, 'upassword': password, 'en_flag': ''}, headers=headers, verify=False, timeout=10)
        
        # 簡單判斷登入是否成功 (可依據學校系統實際回傳調整)
        if "登入失敗" in login_resp.text or "密碼錯誤" in login_resp.text:
            return jsonify({"status": "error", "message": "帳號或密碼錯誤"}), 401

        # 🚀 2. 使用多執行緒 (ThreadPoolExecutor) 同時抓取課表與成績！
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_courses = executor.submit(scrape_courses_and_info, session)
            future_grades = executor.submit(scrape_grades, session)

            student_name, department, course_list = future_courses.result()
            grades_data = future_grades.result()

        if not course_list:
            return jsonify({"status": "error", "message": "找不到課表資料，可能是學校系統維護中"}), 404

        # 3. 取得校曆 (從快取或重新爬取)
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
