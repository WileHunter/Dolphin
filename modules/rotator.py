# proxy_pool/modules/rotator.py
import threading
from collections import defaultdict

class ProxyRotator:
    """代理轮换器."""
    def __init__(self):
        self.all_proxies = []
        self.proxies_by_country = defaultdict(list)
        self.indices = defaultdict(lambda: -1)
        self.current_proxy = None
        self.lock = threading.Lock()

    def clear(self):
        """清空所有代理."""
        with self.lock:
            self.all_proxies = []
            self.proxies_by_country.clear()
            self.indices.clear()
            self.current_proxy = None
            
    def add_proxy(self, proxy_info: dict):
        """添加一个代理."""
        with self.lock:
            proxy_address = proxy_info.get('proxy')
            if any(p.get('proxy') == proxy_address for p in self.all_proxies):
                return 

            self.all_proxies.append(proxy_info)
            country = proxy_info.get('country', 'Unknown')
            self.proxies_by_country[country].append(proxy_info)

    def update_proxy(self, proxy_address: str, updated_info: dict):
        """更新指定代理的信息."""
        with self.lock:
            for p_info in self.all_proxies:
                if p_info.get('proxy') == proxy_address:
                    # 从旧国家分组中移除
                    old_country = p_info.get('country', 'Unknown')
                    if old_country in self.proxies_by_country:
                        try:
                            self.proxies_by_country[old_country].remove(p_info)
                            if not self.proxies_by_country[old_country]:
                                del self.proxies_by_country[old_country]
                        except ValueError:
                            pass
                    # 更新代理信息
                    p_info.update(updated_info)
                    # 添加到新国家分组
                    new_country = updated_info.get('country', 'Unknown')
                    self.proxies_by_country[new_country].append(p_info)
                    # 如果当前代理被更新，同步更新 current_proxy
                    if self.current_proxy and self.current_proxy.get('proxy') == proxy_address:
                        self.current_proxy = p_info
                    return True
            return False

    def remove_proxy(self, proxy_address: str):
        """通过地址删除代理."""
        with self.lock:
            proxy_to_remove = None
            for p_info in self.all_proxies:
                if p_info.get('proxy') == proxy_address:
                    proxy_to_remove = p_info
                    break
            
            if proxy_to_remove:
                self.all_proxies.remove(proxy_to_remove)
                
                country = proxy_to_remove.get('country', 'Unknown')
                if country in self.proxies_by_country:
                    try:
                        self.proxies_by_country[country].remove(proxy_to_remove)
                        if not self.proxies_by_country[country]:
                            del self.proxies_by_country[country]
                    except ValueError:
                        pass
                
                if self.current_proxy == proxy_to_remove:
                    self.current_proxy = None
                return True
            return False

    def get_working_proxies_count(self) -> int:
        """获取可用代理总数（状态为 'Working' 的代理）。"""
        with self.lock:
            return sum(1 for p in self.all_proxies if p.get('status') == 'Working')

    def get_available_regions_with_counts(self, premium_only=False) -> dict:
        """获取各区域的代理数量。"""
        with self.lock:
            counts = {}
            for region, proxies in self.proxies_by_country.items():
                if not proxies:
                    continue
                if premium_only:
                    count = sum(1 for p in proxies if p.get('latency', float('inf')) < 2.0 and p.get('status') == 'Working')
                    if count > 0:
                        counts[region] = count
                else:
                    count = sum(1 for p in proxies if p.get('status') == 'Working')
                    if count > 0:
                        counts[region] = count
            return counts

    def get_next_proxy(self, region="全部国家", premium_only=False):
        """获取下一个代理（仅限状态为 'Working' 的代理）。"""
        with self.lock:
            source_list = []
            region_key = region
            
            if region == "全部国家":
                source_list = self.all_proxies
            elif region in self.proxies_by_country:
                source_list = self.proxies_by_country[region]
            else: 
                source_list = self.all_proxies
                region_key = "全部国家"

            target_list = [
                p for p in source_list 
                if p.get('status') == 'Working' and (not premium_only or p.get('latency', float('inf')) < 2.0)
            ]

            if not target_list:
                self.current_proxy = None
                return None

            index_key = f"{region_key}_{'premium' if premium_only else 'all'}"
            current_idx = self.indices.get(index_key, -1)
            next_idx = (current_idx + 1) % len(target_list)
            self.indices[index_key] = next_idx
            
            self.current_proxy = target_list[next_idx]
            return self.current_proxy

    def get_current_proxy(self):
        """获取当前代理。"""
        with self.lock:
            return self.current_proxy

    def set_current_proxy_by_address(self, proxy_address: str):
        """通过地址设置当前代理（仅限状态为 'Working' 的代理）。"""
        with self.lock:
            for p_info in self.all_proxies:
                if p_info.get('proxy') == proxy_address and p_info.get('status') == 'Working':
                    self.current_proxy = p_info
                    return p_info
            return None