import json
import os
import re
import requests
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# 常量定义
FIGHTS_URL_PREFIX = "https://cn.fflogs.com/v1/report/fights/"
CASTS_URL_PREFIX = "https://cn.fflogs.com/v1/report/events/casts/"
SUMMARY_URL_PREFIX = "https://cn.fflogs.com/v1/report/events/summary/"
DAMAGE_URL_PREFIX = "https://cn.fflogs.com/v1/report/events/damage-taken/"

class RuntimeConfig:
    def __init__(self, logs_id, fight_id, api_key):
        self.logs_id = logs_id
        self.fight_id = fight_id
        self.api_key = api_key
        self.convert_dic = {} 

class Fight:
    def __init__(self, start_time, end_time, fight_id):
        self.start_time = start_time
        self.end_time = end_time
        self.fight_id = fight_id

class Marker:
    def __init__(self, time, marker_type, duration, desc, source, raw):
        self.time = time
        self.marker_type = marker_type
        self.duration = duration
        self.desc = desc
        self.source = source
        self.raw = raw
        self.color = "#217ff5"
        self.show_text = True
        self.track = 0

    def to_dict(self):
        return {
            "time": self.time / 1000,
            "markerType": self.marker_type,
            "duration": self.duration / 1000,
            "description": self.desc,
            "color": self.color,
            "showText": self.show_text
        }

    def get_cast_end_time(self):
        return self.time + self.duration

# --- 核心逻辑处理 ---

def parse_url(url):
    """从URL解析 LOGS_ID 和 FIGHT_ID"""
    log_match = re.search(r'reports/([a-zA-Z0-9]+)', url)
    fight_match = re.search(r'fight=([^&]+)', url)

    if not log_match:
        raise ValueError("无法从链接中解析出 Logs ID")
    
    logs_id = log_match.group(1)
    
    if fight_match:
        fight_val = fight_match.group(1)
        if fight_val == "last":
            fight_id = "last"
        else:
            try:
                fight_id = int(fight_val)
            except ValueError:
                fight_id = 0
    else:
        fight_id = 0
    
    return logs_id, fight_id

def get_fight_data(config):
    url = f"{FIGHTS_URL_PREFIX}{config.logs_id}?api_key={config.api_key}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
        
        fights = data.get('fights', [])
        
        if config.fight_id == "last":
            fight_data = fights[-1] if fights else None
        else:
            fight_data = next((fight for fight in fights if fight['id'] == config.fight_id), None)
            
        if fight_data is None:
            return None
        return Fight(fight_data["start_time"], fight_data["end_time"], fight_data["id"])
    except Exception as e:
        print(f"获取战斗数据失败: {e}")
        return None

# 【新增函数】：全局获取真实的开怪时间偏移量
def get_real_fight_offset(fight, config):
    """
    寻找第一个 'damage' 事件的时间戳作为真实的开怪时间(0秒)。
    如果找不到，则兜底使用 fight.start_time。
    """
    # 请求前 5 秒的数据通常足够找到第一刀
    # 为了保险，这里 end 设置稍微宽一点，比如 start + 10秒，避免还没摸到Boss
    search_end = fight.start_time + 5000 
    
    damage_url = f"{DAMAGE_URL_PREFIX}{config.logs_id}?start={fight.start_time}&end={search_end}&hostility=1&api_key={config.api_key}"
    
    try:
        response = requests.get(damage_url)
        response.raise_for_status()
        data = response.json()
        events = data.get('events', [])
        
        for event in events:
            if event.get('type') == 'damage':
                return event['timestamp']
    except Exception as e:
        print(f"获取开怪锚点失败，将使用默认start_time: {e}")
    
    # 如果没找到 damage 事件，或者请求失败，回退到默认的 fight.start_time
    return fight.start_time

# 【修改】：接收 time_offset 参数
def get_cast_source(fight, config, time_offset):
    # 1. 获取原始数据 
    url = f"{CASTS_URL_PREFIX}{config.logs_id}?start={fight.start_time}&end={fight.end_time}&hostility=1&api_key={config.api_key}"
    
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"获取Cast数据失败: {e}")
        return []

    events = data.get('events', [])
    clean_events = []
    
    # --- 第1轮过滤 ---
    for i, event in enumerate(events):
        name = event.get('ability', {}).get('name', '')
        if 'unknown' in name.lower():
            continue
            
        clean_events.append({
            'original_index': i,
            'event': event,
            'timestamp': event['timestamp'],
            'sourceInstance': event.get('sourceInstance', 0),
            'ability_name': name,
            'to_delete': False
        })

    # --- 第2轮逻辑：处理同时施法 ---
    group_map = {}
    for item in clean_events:
        key = (item['timestamp'], item['ability_name'])
        if key not in group_map:
            group_map[key] = []
        group_map[key].append(item)
        
    hidden_cleanup_tasks = []

    for key, items in group_map.items():
        if len(items) > 1:
            items.sort(key=lambda x: x['sourceInstance'])
            hidden_units = items[1:]
            for hidden in hidden_units:
                if hidden.get('event',{}).get('type','cast') == 'begincast':
                    hidden['to_delete'] = True
                    hidden_cleanup_tasks.append({
                        'sourceInstance': hidden['sourceInstance'],
                        'ability_name': hidden['ability_name'],
                        'search_start_idx': clean_events.index(hidden) + 1,
                        'origin_time': hidden['timestamp'],
                        'origin_duration': hidden['event'].get('duration', 0)
                    })

    # --- 第3轮逻辑：执行"连带消除" ---
    for task in hidden_cleanup_tasks:
        s_id = task['sourceInstance']
        a_name = task['ability_name']
        start_idx = task['search_start_idx']
        window_start = task['origin_time'] + task['origin_duration']
        window_end = window_start + 3000

        for i in range(start_idx, len(clean_events)):
            target_item = clean_events[i]
            target_ts = target_item['timestamp']

            # if target_ts > window_end:
            #     break
            
            if (target_item['sourceInstance'] == s_id and 
                target_item['ability_name'] == a_name):

                target_item['to_delete'] = True

                break

    # --- 第4轮：生成最终 Marker 列表 ---
    source = []
    
    
    for item in clean_events:
        if item['to_delete']:
            continue
            
        event = item['event']
        desc = config.convert_dic.get(item['ability_name'], item['ability_name'])
        duration = event.get('duration', 0)
        timestamp = item['timestamp']
        
        if duration > 0  and duration < 500:
             continue
        
        # 使用传入的 time_offset 进行计算
        m = Marker(timestamp - time_offset, "Info", duration, desc, "casts", event)
        source.append(m)

    # 最后的简单去重
    final_source = []
    cast_ignore_time = 100 
    last_marker = None
    
    for marker in source:
        if (last_marker is not None and marker.desc == last_marker.desc and marker.duration == last_marker.duration
                and marker.time - last_marker.time < cast_ignore_time):
            continue
        final_source.append(marker)
        last_marker = marker

    return final_source


def get_untargetable_list(fight, config, time_offset):
    source = []
    # 【修改】：不再使用 fight.start_time，而是使用传入的 time_offset
    
    events_list = []

    # --- 1. 获取 Targetability ---
    summary_url = f"{SUMMARY_URL_PREFIX}{config.logs_id}?start={fight.start_time}&end={fight.end_time}&api_key={config.api_key}"
    try:
        resp = requests.get(summary_url)
        resp.raise_for_status()
        summary_data = resp.json()
        
        for e in summary_data.get('events', []):
            if 'targetable' in e:
                val = 1 if e['targetable'] == 1 else -1
                events_list.append({
                    'timestamp': e['timestamp'],
                    'type': 'targetability',
                    'val': val,
                    'raw': e,
                    'targetID': e.get('sourceID', e.get('targetID', 0))
                })
    except Exception as e:
        print(f"获取Summary数据失败: {e}")
        return []

    # --- 2. 获取 Overkill ---
    filter_exp = "overkill>0"
    damage_url = f"{DAMAGE_URL_PREFIX}{config.logs_id}?start={fight.start_time}&end={fight.end_time}&hostility=1&filter={filter_exp}&api_key={config.api_key}"
    try:
        resp = requests.get(damage_url)
        resp.raise_for_status()
        damage_data = resp.json()
        for e in damage_data.get('events', []):
            events_list.append({
                'timestamp': e['timestamp'],
                'type': 'overkill',
                'val': -1,
                'raw': e,
                'targetID': e.get('targetID', 0)
            })
    except Exception:
        pass

    # --- 3. 排序与计数逻辑 ---
    events_list.sort(key=lambda x: x['timestamp'])

    count = 1
    current_zero_start_time = None
    current_zero_start_event = None
    dead_units = set()

    for event in events_list:
        prev_count = count
        if event['type'] == 'targetability':
            count += event['val']
        elif event['type'] == 'overkill':
            t_id = event['targetID']
            if t_id not in dead_units:
                count += event['val']
                dead_units.add(t_id)
            
        if count < 0:
            count = 0
            
        if prev_count > 0 and count == 0:
            current_zero_start_time = event['timestamp']
            current_zero_start_event = event['raw']
            
        elif prev_count == 0 and count > 0:
            if current_zero_start_time is not None:
                end_time = event['timestamp']
                duration = end_time - current_zero_start_time
                if duration > 100:
                     m = Marker(
                        current_zero_start_time - time_offset,
                        "Info", 
                        duration, 
                        "不可选中", 
                        "untargetable", 
                        current_zero_start_event
                    )
                     m.color = "#b7b7b7"
                     source.append(m)
                current_zero_start_time = None
                current_zero_start_event = None

    if count == 0 and current_zero_start_time is not None:
        duration = fight.end_time - current_zero_start_time
        if duration > 0:
             m = Marker(
                current_zero_start_time - time_offset, 
                "Info", 
                duration, 
                "不可选中", 
                "untargetable", 
                current_zero_start_event
            )
             m.color = "#b7b7b7"
             source.append(m)

    return source

def convert_marker_list(marker_list):
    return [marker.to_dict() for marker in marker_list]

def make_track_list(info_list):
    min_marker_interval_time = 1000 
    marker_list_dic = {}
    info_list.sort(key=lambda x: x.time)

    for marker in info_list:
        track = 0
        marker_list = marker_list_dic.get(track, [])
        last_end_time = marker_list[-1].get_cast_end_time() if marker_list else 0
        
        while marker.time - last_end_time < min_marker_interval_time:
            track += 1
            marker_list = marker_list_dic.get(track, [])
            last_end_time = marker_list[-1].get_cast_end_time() if marker_list else 0

        marker.track = track
        marker_list.append(marker)
        marker_list_dic[track] = marker_list

    track_list = []
    for track, m_list in marker_list_dic.items():
        track_list.append({
            "fileType": "MarkerTrackIndividual", 
            "track": track, 
            "markers": convert_marker_list(m_list)
        })
    return track_list

def generate_json_data(logs_url, api_key):
    try:
        logs_id, fight_id = parse_url(logs_url)
    except ValueError as e:
        return None, str(e)

    config = RuntimeConfig(logs_id, fight_id, api_key)
    
    fight = get_fight_data(config)
    if fight is None:
        return None, "找不到对应战斗数据 (Fight ID 错误或 API Key 无效)"


    time_offset = get_real_fight_offset(fight, config)

    cast_list = get_cast_source(fight, config, time_offset)
    untarget_list = get_untargetable_list(fight, config, time_offset)

    untargetable_track = {
        "fileType": "MarkerTrackIndividual", 
        "track": -1,
        "markers": convert_marker_list(untarget_list)
    }

    cast_tracks = make_track_list(cast_list)
    final_tracks = [untargetable_track] + cast_tracks
    
    result_json = {
        'fileType': "MarkerTracksCombined", 
        "tracks": final_tracks
    }
    
    return result_json, "Success"

# --- GUI 界面 (保持不变) ---

class Application(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FFLogs Timeline Generator")
        self.geometry("500x350")
        
        self.generated_data = None 

        self.create_widgets()

    def create_widgets(self):
        padding = {'padx': 10, 'pady': 5}

        tk.Label(self, text="FFLogs URL (带 fight id):").pack(anchor='w', **padding)
        self.url_entry = tk.Entry(self, width=60)
        self.url_entry.pack(fill='x', **padding)

        tk.Label(self, text="FFLogs API Key (v1):").pack(anchor='w', **padding)
        self.api_entry = tk.Entry(self, width=60) 
        self.api_entry.pack(fill='x', **padding)

        self.btn_generate = tk.Button(self, text="分析并生成数据", command=self.on_generate, bg="#dddddd")
        self.btn_generate.pack(pady=15)

        self.status_label = tk.Label(self, text="准备就绪", fg="gray")
        self.status_label.pack()

        ttk.Separator(self, orient='horizontal').pack(fill='x', pady=10)

        btn_frame = tk.Frame(self)
        btn_frame.pack(fill='x', pady=10)

        self.btn_copy = tk.Button(btn_frame, text="复制到剪贴板", command=self.on_copy, state='disabled')
        self.btn_copy.pack(side='left', expand=True, padx=5)

        self.btn_save = tk.Button(btn_frame, text="另存为 JSON...", command=self.on_save, state='disabled')
        self.btn_save.pack(side='right', expand=True, padx=5)

    def on_generate(self):
        url = self.url_entry.get().strip()
        api_key = self.api_entry.get().strip()

        if not url or not api_key:
            messagebox.showwarning("提示", "请输入 URL 和 API Key")
            return

        self.status_label.config(text="正在从 FFLogs 获取数据...", fg="blue")
        self.update()

        json_obj, msg = generate_json_data(url, api_key)

        if json_obj is None:
            self.status_label.config(text=f"失败: {msg}", fg="red")
            messagebox.showerror("错误", msg)
        else:
            self.generated_data = json.dumps(json_obj, ensure_ascii=False, indent=2)
            self.status_label.config(text=f"生成成功! 包含 {len(json_obj['tracks'])} 个轨道", fg="green")
            self.btn_copy.config(state='normal')
            self.btn_save.config(state='normal')

    def on_copy(self):
        if self.generated_data:
            self.clipboard_clear()
            self.clipboard_append(self.generated_data)
            self.update() 
            messagebox.showinfo("成功", "JSON 已复制到剪贴板")

    def on_save(self):
        if not self.generated_data:
            return
        
        filepath = filedialog.asksaveasfilename(
            defaultextension=".json",
            filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
            initialfile="timeline.json"
        )
        
        if filepath:
            try:
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(self.generated_data)
                messagebox.showinfo("成功", f"文件已保存至:\n{filepath}")
            except Exception as e:
                messagebox.showerror("保存失败", str(e))

if __name__ == '__main__':
    app = Application()
    app.mainloop()
