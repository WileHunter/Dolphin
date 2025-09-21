# modules/checker.py

import requests
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import subprocess
import re

class ProxyChecker:
    """
    一个优化的、多阶段的代理验证器。
    [!] 优化: 公网IP通过调用系统curl获取，并只为低延迟代理测速。
    [!] 新增: 从 myip.ipip.net 获取国家（country）和城市（city）信息。
    """
    def __init__(self, timeout: int = 5):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Safari/537.36"
        })
        
        self.validation_targets = {
            'latency_check': 'https://www.baidu.com',
            'anonymity_check': 'http://httpbin.org/get?show_env=1',
            'speed_check': 'http://cachefly.cachefly.net/100kb.test',
            'location_check': 'http://myip.ipip.net',  # [!] 改为 location_check，用于获取国家和城市
        }
        
        self.public_ip = None
        self.log_queue = None  # [!] 初始化 log_queue

    def initialize_public_ip(self, log_queue=None):
        """[!] 优化: 使用subprocess模块异步调用系统的curl命令获取IP。"""
        try:
            command = ['curl', 'ip.sb']
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=True,
                timeout=10
            )
            ip_address = result.stdout.strip()
            
            if ip_address and '.' in ip_address:
                self.public_ip = ip_address
                if log_queue:
                    log_queue.put(f"[Checker] 成功获取本机公网IP: {self.public_ip} (通过 ip.sb)")
            else:
                if log_queue:
                    log_queue.put(f"[Checker] [!] 调用curl ip.sb未能返回有效IP。响应: '{ip_address}'")

        except FileNotFoundError:
            if log_queue:
                log_queue.put("[Checker] [!] 'curl'命令未找到。请确保curl已安装并在系统PATH中。")
        except Exception as e:
            if log_queue:
                log_queue.put(f"[Checker] [!] 调用系统curl获取本机公网IP失败: {e}")

    # --- 代理验证核心逻辑 ---
    
    def _pre_check_proxy(self, proxy: str):
        try:
            ip, port_str = proxy.split(':')
            with socket.create_connection((ip, int(port_str)), timeout=1.5):
                return True
        except Exception:
            return False

    def _full_check_proxy(self, proxy_info: dict, validation_mode: str = 'online'):
        proxy = proxy_info['proxy']
        protocol = proxy_info['protocol']
        proxy_url = f"{protocol.lower()}://{proxy}"
        proxies_dict = {'http': proxy_url, 'https': proxy_url}
        result = {
            'proxy': proxy, 'protocol': protocol.upper(), 'status': 'Failed',
            'latency': float('inf'), 'speed': 0, 'anonymity': 'Unknown', 'country': 'N/A', 'city': 'N/A'
        }
        try:
            start_time = time.time()
            self.session.head(self.validation_targets['latency_check'], proxies=proxies_dict, timeout=self.timeout).raise_for_status()
            result['latency'] = time.time() - start_time

            res_anon = self.session.get(self.validation_targets['anonymity_check'], proxies=proxies_dict, timeout=self.timeout)
            res_anon.raise_for_status()
            data = res_anon.json()
            origin_ips_str = data.get('headers', {}).get('X-Forwarded-For', data.get('origin', ''))
            origin_ips = [ip.strip() for ip in origin_ips_str.split(',')]
            
            if self.public_ip and any(self.public_ip in ip for ip in origin_ips):
                result['anonymity'] = 'Transparent'
                return result
            elif len(origin_ips) > 1 or 'Via' in data.get('headers', {}):
                result['anonymity'] = 'Anonymous'
            else:
                result['anonymity'] = 'Elite'

            # [!] 新增：从 myip.ipip.net 获取国家和城市信息
            try:
                location_response = self.session.get(self.validation_targets['location_check'], proxies=proxies_dict, timeout=self.timeout)
                location_response.raise_for_status()
                location_text = location_response.text
                # 正则匹配：来自于：国家 [城市] [运营商]
                location_match = re.search(r'来自于：(\S+)(.*)', location_text)
                if location_match:
                    result['country'] = location_match.group(1).strip()
                    result['city'] = location_match.group(2).strip() if location_match.group(2) else 'Unknown'
                else:
                    result['country'] = 'Unknown'
                    result['city'] = 'Unknown'
                    if self.log_queue:
                        self.log_queue.put(f"[Checker] 无法从 {proxy} 的响应中解析国家和城市: {location_text}")
            except Exception as e:
                result['country'] = 'Unknown'
                result['city'] = 'Unknown'
                if self.log_queue:
                    self.log_queue.put(f"[Checker] 获取 {proxy} 的国家和城市信息失败: {e}")

            # [!] 优化: 仅当延迟低于7秒时才进行速度测试
            if result['latency'] <= 7.0:
                speed_check_url = self.validation_targets['latency_check'] if validation_mode == 'online' else self.validation_targets['speed_check']
                try:
                    start_speed = time.time()
                    speed_response = self.session.get(speed_check_url, proxies=proxies_dict, timeout=15, stream=True)
                    speed_response.raise_for_status()
                    
                    content_size = 0
                    for chunk in speed_response.iter_content(chunk_size=8192):
                        content_size += len(chunk)

                    speed_duration = time.time() - start_speed
                    if speed_duration > 0 and content_size > 0:
                        result['speed'] = (content_size / speed_duration) * 8 / (1000**2)
                except Exception:
                    pass # 测速失败，速度保持为0

            result['status'] = 'Working'
            return result

        except requests.RequestException:
            return result

    def validate_all(self, proxies_by_protocol: dict, result_queue, log_queue, validation_mode='online'):
        self.log_queue = log_queue
        all_proxies_flat = [{'proxy': p, 'protocol': proto} for proto, proxies in proxies_by_protocol.items() for p in proxies]
        total_proxies = len(all_proxies_flat)
        
        survivors = []
        if total_proxies > 10000:
            log_queue.put(f"[!] 代理总数 ({total_proxies}) 超过10000，跳过TCP预检。")
            survivors = all_proxies_flat
        else:
            log_queue.put(f"[*] 阶段一：TCP预检开始，总数: {total_proxies}...")
            with ThreadPoolExecutor(max_workers=500) as executor:
                future_to_proxy = {executor.submit(self._pre_check_proxy, p['proxy']): p for p in all_proxies_flat}
                for future in as_completed(future_to_proxy):
                    if future.result():
                        survivors.append(future_to_proxy[future])
            log_queue.put(f"[+] 阶段一：TCP预检完成，幸存者: {len(survivors)} / {total_proxies}。")
        
        log_queue.put("\n" + "="*20 + f" 阶段二：开始完整质量验证 " + "="*20)
        
        if not survivors:
            result_queue.put(None)
            return

        with ThreadPoolExecutor(max_workers=100) as executor:
            futures = [executor.submit(self._full_check_proxy, p, validation_mode) for p in survivors]
            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        result_queue.put(result)
                except Exception as e:
                    log_queue.put(f"[!] 验证器线程出现异常: {e}")

        result_queue.put(None)