import json
import os
import re
import requests
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

# --- 常量定义 ---
FIGHTS_URL_PREFIX = "https://cn.fflogs.com/v1/report/fights/"
CASTS_URL_PREFIX = "https://cn.fflogs.com/v1/report/events/casts/"
SUMMARY_URL_PREFIX = "https://cn.fflogs.com/v1/report/events/summary/"
DAMAGE_URL_PREFIX = "https://cn.fflogs.com/v1/report/events/damage-taken/"
ANY_URL_PREFIX = "https://cn.fflogs.com/v1/report/events/any/" # 之前版本补充的

# --- 基础类定义 ---

class RuntimeConfig:
    def __init__(self, logs_id, fight_id, api_key, translate=False):
        self.logs_id = logs_id
        self.fight_id = fight_id
        self.api_key = api_key
        # 如果勾选翻译，生成对应的URL参数后缀
        self.translate_param = "&translate=true" if translate else ""
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

# --- GUI: 技能配置弹窗 ---

class SkillConfigDialog(tk.Toplevel):
    def __init__(self, parent, skill_list):
        super().__init__(parent)
        self.title("导出配置与技能过滤")
        self.geometry("600x600")
        self.result = None # 用于存储返回的配置
        self.skill_vars = {} # 存储控件变量

        # 设置模态
        self.transient(parent)
        self.grab_set()
        
        # --- 顶部：通用设置 ---
        top_frame = tk.LabelFrame(self, text="通用设置", padx=10, pady=10)
        top_frame.pack(fill='x', padx=10, pady=5)

        tk.Label(top_frame, text="最小间隔(ms):").grid(row=0, column=0, padx=5)
        self.interval_entry = tk.Entry(top_frame, width=10)
        self.interval_entry.insert(0, "1000")
        self.interval_entry.grid(row=0, column=1, padx=5)

        tk.Label(top_frame, text="最大轨道数(Max Tracks):").grid(row=0, column=2, padx=5)
        self.tracks_entry = tk.Entry(top_frame, width=10)
        self.tracks_entry.insert(0, "20")
        self.tracks_entry.grid(row=0, column=3, padx=5)

        # --- 中部：技能列表 (带滚动条) ---
        list_frame = tk.LabelFrame(self, text="技能筛选与重命名 (打钩导出，修改文本框重命名)", padx=5, pady=5)
        list_frame.pack(fill='both', expand=True, padx=10, pady=5)

        # 创建 Canvas 和 Scrollbar
        canvas = tk.Canvas(list_frame)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        self.scrollable_frame = tk.Frame(canvas)

        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # 绑定鼠标滚轮
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        # 填充技能列表
        sorted_skills = sorted(list(skill_list))
        
        # 表头
        tk.Label(self.scrollable_frame, text="导出?", font=('bold')).grid(row=0, column=0, padx=5, pady=5)
        tk.Label(self.scrollable_frame, text="原名", font=('bold')).grid(row=0, column=1, padx=5, pady=5, sticky='w')
        tk.Label(self.scrollable_frame, text="导出名称 (可编辑)", font=('bold')).grid(row=0, column=2, padx=5, pady=5, sticky='w')

        for idx, skill_name in enumerate(sorted_skills):
            row = idx + 1
            
            # 是否导出变量
            check_var = tk.BooleanVar(value=True)
            # 重命名变量
            rename_var = tk.StringVar(value=skill_name)
            
            self.skill_vars[skill_name] = {
                'check': check_var,
                'rename': rename_var
            }

            ck = tk.Checkbutton(self.scrollable_frame, variable=check_var)
            ck.grid(row=row, column=0, padx=5)

            lbl = tk.Label(self.scrollable_frame, text=skill_name)
            lbl.grid(row=row, column=1, padx=10, sticky='w')

            entry = tk.Entry(self.scrollable_frame, textvariable=rename_var, width=30)
            entry.grid(row=row, column=2, padx=10, sticky='w')

        # --- 底部：按钮 ---
        btn_frame = tk.Frame(self, pady=10)
        btn_frame.pack(fill='x')
        
        tk.Button(btn_frame, text="确定生成", command=self.on_ok, bg="#dddddd", width=15).pack(side='right', padx=20)
        tk.Button(btn_frame, text="取消", command=self.destroy, width=15).pack(side='right', padx=20)

    def on_ok(self):
        try:
            min_interval = int(self.interval_entry.get())
            max_tracks = int(self.tracks_entry.get())
        except ValueError:
            messagebox.showerror("错误", "时间间隔和轨道数必须是整数")
            return

        # 收集技能配置
        filter_map = {} # {原名: 新名}，如果不在字典里则说明不导出
        
        for original_name, vars in self.skill_vars.items():
            if vars['check'].get():
                new_name = vars['rename'].get().strip()
                filter_map[original_name] = new_name if new_name else original_name

        self.result = {
            'min_interval': min_interval,
            'max_tracks': max_tracks,
            'filter_map': filter_map
        }
        self.destroy()

# --- 核心逻辑处理 ---

def parse_url(url):
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
    # 【修改】：追加 config.translate_param
    url = f"{FIGHTS_URL_PREFIX}{config.logs_id}?api_key={config.api_key}{config.translate_param}"
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

def get_real_fight_offset(fight, config):
    search_end = fight.start_time + 5000 
    # 【修改】：追加 config.translate_param
    damage_url = f"{DAMAGE_URL_PREFIX}{config.logs_id}?start={fight.start_time}&end={search_end}&hostility=1&api_key={config.api_key}{config.translate_param}"
    try:
        response = requests.get(damage_url)
        response.raise_for_status()
        data = response.json()
        events = data.get('events', [])
        for event in events:
            if event.get('type') == 'damage':
                return event['timestamp']
    except Exception as e:
        print(f"获取开怪锚点失败: {e}")
    return fight.start_time

def get_cast_source(fight, config, time_offset):
    # 【修改】：追加 config.translate_param
    url = f"{CASTS_URL_PREFIX}{config.logs_id}?start={fight.start_time}&end={fight.end_time}&hostility=1&api_key={config.api_key}{config.translate_param}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        print(f"获取Cast数据失败: {e}")
        return []

    events = data.get('events', [])
    clean_events = []
    
    # 第1轮：基础清洗
    for i, event in enumerate(events):
        name = event.get('ability', {}).get('name', '')
        
        clean_events.append({
            'original_index': i,
            'event': event,
            'timestamp': event['timestamp'],
            'sourceInstance': event.get('sourceInstance', 0),
            'ability_name': name,
            'to_delete': False
        })

    # 第2轮：处理同时施法
    group_map = {}
    for item in clean_events:
        key = (item['timestamp'], item['ability_name'])
        if key not in group_map: group_map[key] = []
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

    # 第3轮：连带消除
    for task in hidden_cleanup_tasks:
        s_id = task['sourceInstance']
        a_name = task['ability_name']
        start_idx = task['search_start_idx']
        window_start = task['origin_time'] + task['origin_duration']
        window_end = window_start + 3000

        for i in range(start_idx, len(clean_events)):
            target_item = clean_events[i]
            target_ts = target_item['timestamp']
            if (target_item['sourceInstance'] == s_id and target_item['ability_name'] == a_name):
                target_item['to_delete'] = True
                break

    # 第4轮：生成 Marker 对象
    source = []
    for item in clean_events:
        if item['to_delete']: continue
        
        event = item['event']
        desc = item['ability_name'] # 暂时保持原名，后续处理
        duration = event.get('duration', 0)
        timestamp = item['timestamp']
        
        if duration > 0  and duration < 500: continue
        
        m = Marker(timestamp - time_offset, "Info", duration, desc, "casts", event)
        source.append(m)

    # 简单去重
    final_source = []
    cast_ignore_time = 100 
    last_marker = None
    for marker in source:
        if (last_marker is not None and marker.desc == last_marker.desc and 
            marker.duration == last_marker.duration and 
            marker.time - last_marker.time < cast_ignore_time):
            continue
        final_source.append(marker)
        last_marker = marker

    return final_source

def get_untargetable_list(fight, config, time_offset):
    source = []
    events_list = []

    # 1. Targetability
    filter_exp = 'type="targetabilityupdate"'
    # 【修改】：追加 config.translate_param
    summary_url = f"{SUMMARY_URL_PREFIX}{config.logs_id}?start={fight.start_time}&end={fight.end_time}&filter={filter_exp}&api_key={config.api_key}{config.translate_param}"
    try:
        resp = requests.get(summary_url)
        resp.raise_for_status()
        summary_data = resp.json()
        for e in summary_data.get('events', []):
            if 'targetable' in e:
                val = 1 if e['targetable'] == 1 else -1
                events_list.append({
                    'timestamp': e['timestamp'], 'type': 'targetability', 'val': val,
                    'raw': e, 'targetID': e.get('sourceID', e.get('targetID', 0))   
                })
    except Exception as e:
        print(f"获取Summary数据失败: {e}")

    # 2. Overkill
    filter_exp = "overkill>0"
    # 【修改】：追加 config.translate_param
    damage_url = f"{DAMAGE_URL_PREFIX}{config.logs_id}?start={fight.start_time}&end={fight.end_time}&hostility=1&filter={filter_exp}&api_key={config.api_key}{config.translate_param}"
    try:
        resp = requests.get(damage_url)
        resp.raise_for_status()
        damage_data = resp.json()
        for e in damage_data.get('events', []):
            events_list.append({
                'timestamp': e['timestamp'], 'type': 'overkill', 'val': -1,
                'raw': e, 'targetID': e.get('targetID', 0)
            })
    except Exception:
        pass

    # 3. 去重与计算
    events_list.sort(key=lambda x: x['timestamp'])
    
    # 100ms 窗口去重
    unique_events = []
    last_seen_map = {}
    threshold = 100 
    for event in events_list:
        t_id = event['targetID']
        val = event['val']
        curr_ts = event['timestamp']
        key = (t_id, val)
        last_ts = last_seen_map.get(key)
        if last_ts is not None and (curr_ts - last_ts < threshold):
            continue
        unique_events.append(event)
        last_seen_map[key] = curr_ts
    events_list = unique_events
    
    count = 1
    current_zero_start_time = None
    current_zero_start_event = None
    dead_units = set()

    for event in events_list:
        prev_count = count
        if event['type'] == 'targetability': count += event['val']
        elif event['type'] == 'overkill':
            t_id = event['targetID']
            if t_id not in dead_units:
                count += event['val']
                dead_units.add(t_id)
        if count < 0: count = 0
            
        if prev_count > 0 and count == 0:
            current_zero_start_time = event['timestamp']
            current_zero_start_event = event['raw']
        elif prev_count == 0 and count > 0:
            if current_zero_start_time is not None:
                end_time = event['timestamp']
                duration = end_time - current_zero_start_time
                if duration > 0:
                     m = Marker(current_zero_start_time - time_offset, "Info", duration, "不可选中", "untargetable", current_zero_start_event)
                     m.color = "#b7b7b7"
                     source.append(m)
                current_zero_start_time = None
                current_zero_start_event = None

    if count == 0 and current_zero_start_time is not None:
        duration = fight.end_time - current_zero_start_time
        if duration > 0:
             m = Marker(current_zero_start_time - time_offset, "Info", duration, "不可选中", "untargetable", current_zero_start_event)
             m.color = "#b7b7b7"
             source.append(m)

    return source

# 【修改】：接收 config 以动态设置间隔和最大轨道数
def make_track_list(info_list, min_interval, max_tracks):
    marker_list_dic = {}
    info_list.sort(key=lambda x: x.time)

    for marker in info_list:
        track = 0
        marker_list = marker_list_dic.get(track, [])
        last_end_time = marker_list[-1].get_cast_end_time() if marker_list else 0
        
        # 使用动态传入的间隔
        while marker.time - last_end_time < min_interval:
            track += 1
            marker_list = marker_list_dic.get(track, [])
            last_end_time = marker_list[-1].get_cast_end_time() if marker_list else 0

        marker.track = track
        marker_list.append(marker)
        marker_list_dic[track] = marker_list

    track_list = []
    sorted_tracks = sorted(marker_list_dic.keys())
    
    for track in sorted_tracks:
        # 轨道数限制
        if track >= max_tracks:
            break
        track_list.append({
            "fileType": "MarkerTrackIndividual", 
            "track": track, 
            "markers": convert_marker_list(marker_list_dic[track])
        })
    return track_list

def convert_marker_list(marker_list):
    return [marker.to_dict() for marker in marker_list]

# --- 逻辑拆分 ---

def fetch_log_data(logs_url, api_key, is_translate):
    """阶段1: 只抓取数据，不生成 Tracks"""
    try:
        logs_id, fight_id = parse_url(logs_url)
    except ValueError as e:
        return None, None, str(e)

    # 【修改】：传入 translate 参数
    config = RuntimeConfig(logs_id, fight_id, api_key, translate=is_translate)
    
    fight = get_fight_data(config)
    if fight is None:
        return None, None, "找不到对应战斗数据 (Fight ID 错误或 API Key 无效)"

    time_offset = get_real_fight_offset(fight, config)
    
    # 获取原始 Marker 列表
    cast_list = get_cast_source(fight, config, time_offset)
    untarget_list = get_untargetable_list(fight, config, time_offset)
    
    return cast_list, untarget_list, "Success"

def generate_final_json(cast_list, untarget_list, user_config):
    """阶段2: 根据用户配置过滤并生成 JSON"""
    
    min_interval = user_config['min_interval']
    max_tracks = user_config['max_tracks']
    filter_map = user_config['filter_map'] # {原名: 新名} (不在其中的已被过滤)

    # 1. 过滤和重命名
    final_cast_list = []
    for marker in cast_list:
        # 如果原名不在 map 中，说明用户取消了勾选
        if marker.desc not in filter_map:
            continue
        
        # 重命名
        marker.desc = filter_map[marker.desc]
        final_cast_list.append(marker)

    # 2. 生成轨道
    untargetable_track = {
        "fileType": "MarkerTrackIndividual", 
        "track": -1,
        "markers": convert_marker_list(untarget_list)
    }

    cast_tracks = make_track_list(final_cast_list, min_interval, max_tracks)
    final_tracks = [untargetable_track] + cast_tracks
    
    result_json = {
        'fileType': "MarkerTracksCombined", 
        "tracks": final_tracks
    }
    
    return result_json

# --- GUI 主程序 ---

class Application(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FFLogs Timeline Generator")
        self.geometry("500x380") # 稍微调高高度以容纳新复选框
        
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

        # 【新增】：是否启用翻译的复选框
        self.translate_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="请求时自动翻译 (&translate=true)", variable=self.translate_var).pack(anchor='w', **padding)

        self.btn_generate = tk.Button(self, text="获取数据并配置...", command=self.on_process_start, bg="#dddddd")
        self.btn_generate.pack(pady=15)

        self.status_label = tk.Label(self, text="准备就绪", fg="gray")
        self.status_label.pack()

        ttk.Separator(self, orient='horizontal').pack(fill='x', pady=10)

        btn_frame = tk.Frame(self)
        btn_frame.pack(fill='x', pady=10)

        self.btn_copy = tk.Button(btn_frame, text="复制 JSON", command=self.on_copy, state='disabled')
        self.btn_copy.pack(side='left', expand=True, padx=5)

        self.btn_save = tk.Button(btn_frame, text="另存为文件...", command=self.on_save, state='disabled')
        self.btn_save.pack(side='right', expand=True, padx=5)

    def on_process_start(self):
        url = self.url_entry.get().strip()
        api_key = self.api_entry.get().strip()

        if not url or not api_key:
            messagebox.showwarning("提示", "请输入 URL 和 API Key")
            return

        self.status_label.config(text="正在从 FFLogs 下载数据...", fg="blue")
        self.update()

        # 【修改】：传入 translate_var 的状态
        cast_list, untarget_list, msg = fetch_log_data(url, api_key, self.translate_var.get())

        if cast_list is None:
            self.status_label.config(text=f"下载失败: {msg}", fg="red")
            messagebox.showerror("错误", msg)
            return

        self.status_label.config(text="数据获取成功，等待配置...", fg="orange")
        
        # 2. 提取所有技能名称用于配置
        unique_skills = set(marker.desc for marker in cast_list)
        
        # 3. 弹出配置窗口
        dialog = SkillConfigDialog(self, unique_skills)
        self.wait_window(dialog) # 等待窗口关闭

        if dialog.result is None:
            self.status_label.config(text="用户取消了操作", fg="gray")
            return

        # 4. 生成最终数据
        self.status_label.config(text="正在处理...", fg="blue")
        json_obj = generate_final_json(cast_list, untarget_list, dialog.result)

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
        if not self.generated_data: return
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
