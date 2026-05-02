from flask import Flask, request, jsonify
import requests
from bs4 import BeautifulSoup
import urllib3
import re
import datetime

# 1️⃣ 🏢 定義教室代碼對照表字典
BUILDING_MAP = {
    'AK': '任垣樓 ',
    'SP': '伯鐸樓 ',
    'JA': '靜安樓 ',
    'TG': '格倫樓 ',
    'PH': '主顧樓 ',
    'SF': '方濟樓 ',
    'SY': '思源樓 ',
    '2R': '第二研究大樓 ',
    'AK-3C': '計算機中心 ',
    '1R': '第一研究大樓 ',
    'ST': '體育館 ',
    'SD': '田徑場 '
}

# 2️⃣ 排序代碼 (由長到短)，避免誤判
SORTED_BUILDING_CODES = sorted(BUILDING_MAP.keys(), key=len, reverse=True)

# 關閉橘色的安全警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

# 3️⃣ 輔助函式：用來轉換教室名稱
def format_location(raw_location_str):
    if not raw_location_str:
        return "未知教室"
    
    for code in SORTED_BUILDING_CODES:
        if raw_location_str.startswith(code):
            building_full_name = BUILDING_MAP[code]
            room_number = raw_location_str[len(code):].lstrip(':- ')
            if room_number:
                return f"{building_full_name} ({room_number})"
            else:
                return building_full_name
            
    return raw_location_str

# 4️⃣ 主要的 API 路由：課表同步
@app.route('/api/sync_campus', methods=['POST'])
def sync_campus():
    data = request.json
    student_id = data.get('student_id')
    password = data.get('password')

    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }

    login_url = "https://alcat.pu.edu.tw/index_check.php"
    payload = {
        'uid': student_id,
        'upassword': password,
        'en_flag': ''
    }

    try:
        login_resp = session.post(login_url, data=payload, headers=headers, verify=False, timeout=10)
        login_resp.encoding = 'utf-8'
        
        course_url = "https://alcat.pu.edu.tw/stu_query/query_course.html"
        course_resp = session.get(course_url, headers=headers, verify=False, timeout=10)
        course_resp.encoding = 'utf-8'

        soup = BeautifulSoup(course_resp.text, 'html.parser')
        course_table = soup.find('table', class_='small')
        
        if not course_table:
            return jsonify({"status": "error", "message": "找不到課表資料，請檢查帳密是否正確"}), 401
            
        rows = course_table.find_all('tr')
        course_list = []
        
        for row in rows[1:]:
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
                    except:
                        pass
                
                full_location = format_location(raw_location)
                full_title = cols[2].get_text(separator='\n', strip=True)
                short_title = full_title.split('\n')[0]

                course_data = {
                    "title": short_title,
                    "weekday": weekday,
                    "sessions": sessions,
                    "location": full_location 
                }
                course_list.append(course_data)

        return jsonify({
            "status": "success",
            "message": f"成功同步 {len(course_list)} 門課程！",
            "courses": course_list
        })

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"status": "error", "message": f"伺服器錯誤: {str(e)}"}), 500

# 5️⃣ 天氣 API 路由 (必須放在 app.run 的上面)
# 5️⃣ 天氣 API 路由 (更強大的容錯版本)
# 5️⃣ 天氣 API 路由 (終極 Regex 暴力破解版)
@app.route('/api/weather', methods=['GET'])
def get_weather():
    cwa_api_key = "CWA-706D5143-2567-4EC1-9FC5-FDB6079B736B" 
    url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-D0047-073?Authorization={cwa_api_key}&locationName=沙鹿區"

    try:
        response = requests.get(url, timeout=5, verify=False)
        response.encoding = 'utf-8'
        text_data = response.text  # 直接把回傳結果當作一整串純文字

        # 🔍 終極絕招：用正規表達式直接從字串中「挖」出數字
        # 尋找 "溫度攝氏XX度" 並抽出裡面的數字
        temp_match = re.search(r'溫度攝氏(\d+)度', text_data)
        # 尋找 "降雨機率XX%" 並抽出裡面的數字
        pop_match = re.search(r'降雨機率(\d+)%', text_data)

        temperature = temp_match.group(1) if temp_match else "26"
        pop = pop_match.group(1) if pop_match else "10"

        return jsonify({
            "status": "success",
            "location": "沙鹿區",
            "temperature": f"{temperature}°C",
            "pop": f"{pop}%"
        })

    except Exception as e:
        print(f"⚠️ 抓取天氣失敗: {e}")
        return jsonify({
            "status": "error",
            "location": "沙鹿區",
            "temperature": "26°C", 
            "pop": "10%"
        })
# 6️⃣ 今明兩天天氣 API (自動分群防呆版)
@app.route('/api/weather_weekly', methods=['GET'])
def get_weekly_weather():
    cwa_api_key = "CWA-706D5143-2567-4EC1-9FC5-FDB6079B736B" 
    url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001?Authorization={cwa_api_key}&locationName=臺中市"

    try:
        response = requests.get(url, timeout=5, verify=False)
        data = response.json()

        locations = data.get('records', {}).get('location', [])
        if not locations:
            return jsonify({"status": "error", "message": "氣象署回傳空資料"})

        weather_elements = locations[0].get('weatherElement', [])
        elements_dict = {el['elementName']: el['time'] for el in weather_elements}

        times = elements_dict.get('Wx', [])
        if not times:
            return jsonify({"status": "error", "message": "找不到時間資料"})

        # 計算今天與明天的精確日期
        base_date_str = times[0]['startTime'][:10] 
        base_date_obj = datetime.datetime.strptime(base_date_str, "%Y-%m-%d")
        tomorrow_obj = base_date_obj + datetime.timedelta(days=1)
        
        today_formatted = base_date_obj.strftime("%m/%d")
        tomorrow_formatted = tomorrow_obj.strftime("%m/%d")

        today_data = []
        tomorrow_data = []

        for i in range(len(times)):
            start_time = times[i]['startTime']
            end_time = times[i]['endTime']
            current_date = start_time[:10]

            start_h = int(start_time[11:13])
            end_h = int(end_time[11:13])

            # 重新精準命名 12 小時的時段
            if start_h == 0 and end_h == 6:
                time_label = "凌晨"
            elif start_h == 6 and end_h == 18:
                time_label = "白天"
            elif start_h == 18 and end_h == 6:
                time_label = "晚上"
            else:
                time_label = f"{start_h}:00 開始"

            wx = elements_dict['Wx'][i]['parameter']['parameterName']
            min_t = elements_dict['MinT'][i]['parameter']['parameterName']
            max_t = elements_dict['MaxT'][i]['parameter']['parameterName']
            pop = elements_dict['PoP'][i]['parameter']['parameterName']

            weather_info = {
                "time": time_label,
                "min_t": min_t,
                "max_t": max_t,
                "wx": wx,
                "pop": f"{pop}%"
            }

            if current_date == base_date_str:
                today_data.append(weather_info)
            else:
                tomorrow_data.append(weather_info)

        # 💡 終極防呆機制：如果半夜時段抓不到明天的白天資料，自動補上一張等待更新的小卡片！
        if len(tomorrow_data) == 0:
            tomorrow_data.append({
                "time": "白天 (稍晚發布)",
                "min_t": "--",
                "max_t": "--",
                "wx": "等待氣象署清晨更新",
                "pop": "--%"
            })

        return jsonify({
            "status": "success",
            "today_date": today_formatted,
            "today": today_data,
            "tomorrow_date": tomorrow_formatted,
            "tomorrow": tomorrow_data
        })

    except Exception as e:
        print(f"⚠️ 今明天氣抓取失敗: {e}")
        return jsonify({"status": "error", "message": "伺服器內部錯誤"})
    
        # 🛑 啟動伺服器的代碼，必須永遠待在整份檔案的「最後一行」
if __name__ == '__main__':
    # host='0.0.0.0' 代表接受來自區域網路內所有 IP 的連線
    app.run(host='0.0.0.0', port=5000, debug=True)
