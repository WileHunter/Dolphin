# modules/server.py

import socket
import threading
import select
import struct
import socks 
from urllib.parse import urlparse

class ProxyServer:
    """HTTP/SOCKS5 代理服务."""
    def __init__(self, http_host, http_port, socks5_host, socks5_port, rotator, log_queue):
        self._rotator = rotator
        self._log_queue = log_queue
        self._running = False

        self._http_host = http_host
        self._http_port = http_port
        self._http_server_socket = None
        self._http_thread = None

        self._socks5_host = socks5_host
        self._socks5_port = socks5_port
        self._socks5_server_socket = None
        self._socks5_thread = None

    def log(self, message):
        self._log_queue.put(f"[Server] {message}")

    def start_all(self):
        """启动HTTP和SOCKS5服务."""
        if self._running:
            return
        self._running = True

        self._http_thread = threading.Thread(target=self._run_http_server, daemon=True)
        self._http_thread.start()

        self._socks5_thread = threading.Thread(target=self._run_socks5_server, daemon=True)
        self._socks5_thread.start()

    def stop_all(self):
        """停止所有服务."""
        if not self._running:
            return
        self._running = False
        
        # 关闭服务端口以中断accept()
        if self._http_server_socket:
            self._http_server_socket.close()
        if self._socks5_server_socket:
            self._socks5_server_socket.close()

        # 等待线程结束
        if self._http_thread and self._http_thread.is_alive():
            self._http_thread.join()
        if self._socks5_thread and self._socks5_thread.is_alive():
            self._socks5_thread.join()
            
        self.log("所有代理服务已停止。")

    def _run_http_server(self):
        """HTTP服务主循环."""
        try:
            self._http_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._http_server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._http_server_socket.bind((self._http_host, self._http_port))
            self._http_server_socket.listen(20)
            self.log(f"HTTP 代理服务接口已启动于 {self._http_host}:{self._http_port}")
        except Exception as e:
            self.log(f"[!] 启动 HTTP 服务失败: {e}")
            return

        while self._running:
            try:
                client_socket, _ = self._http_server_socket.accept()
                handler = threading.Thread(target=self._handle_http_client, args=(client_socket,), daemon=True)
                handler.start()
            except OSError:
                # 关闭socket时会产生OSError, 正常退出
                break 
        self.log("HTTP 代理服务循环已退出。")

    def _run_socks5_server(self):
        """SOCKS5服务主循环."""
        try:
            self._socks5_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socks5_server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._socks5_server_socket.bind((self._socks5_host, self._socks5_port))
            self._socks5_server_socket.listen(20)
            self.log(f"SOCKS5 代理服务接口已启动于 {self._socks5_host}:{self._socks5_port}")
        except Exception as e:
            self.log(f"[!] 启动 SOCKS5 服务失败: {e}")
            return

        while self._running:
            try:
                client_socket, _ = self._socks5_server_socket.accept()
                handler = threading.Thread(target=self._handle_socks5_client, args=(client_socket,), daemon=True)
                handler.start()
            except OSError:
                break
        self.log("SOCKS5 代理服务循环已退出。")
        
    def _get_upstream_connection(self, target_host, target_port):
        """通过上游代理连接目标."""
        upstream_proxy_info = self._rotator.get_current_proxy()
        if not upstream_proxy_info:
            self.log("[!] 代理池为空，无法转发请求。")
            return None

        # 安全地获取代理信息
        addr = upstream_proxy_info.get('proxy')
        proto = upstream_proxy_info.get('protocol')

        if not addr or not proto:
            self.log(f"[!] 代理信息格式不正确: {upstream_proxy_info}")
            return None

        upstream_addr, upstream_port_str = addr.split(':')
        
        proxy_type_map = {'HTTP': socks.HTTP, 'SOCKS4': socks.SOCKS4, 'SOCKS5': socks.SOCKS5}
        upstream_protocol = proxy_type_map.get(proto.upper())

        if not upstream_protocol:
            self.log(f"[!] 不支持的上游代理协议: {proto}")
            return None
        
        remote_socket = socks.socksocket()
        try:
            remote_socket.set_proxy(proxy_type=upstream_protocol, addr=upstream_addr, port=int(upstream_port_str))
            remote_socket.connect((target_host, target_port))
            self.log(f"通过 {addr} 连接到 {target_host}:{target_port}")
            return remote_socket
        except Exception as e:
            self.log(f"[!] 上游代理 {addr} 错误: {e}")
            remote_socket.close()
            return None

    def _handle_http_client(self, client_socket):
        """处理HTTP客户端请求."""
        remote_socket = None
        try:
            request_data = client_socket.recv(8192)
            if not request_data:
                return

            first_line = request_data.split(b'\r\n')[0].decode('utf-8', 'ignore')
            method, url, _ = first_line.split()

            if method == 'CONNECT':
                target_host, target_port_str = url.split(':')
                target_port = int(target_port_str)
            else:
                parsed_url = urlparse(url)
                target_host = parsed_url.hostname
                target_port = parsed_url.port or 80

            remote_socket = self._get_upstream_connection(target_host, target_port)
            if not remote_socket:
                return

            if method == 'CONNECT':
                client_socket.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            else:
                remote_socket.sendall(request_data)

            self._forward_data(client_socket, remote_socket)
        except Exception as e:
            # 忽略常见的连接断开错误
            if not isinstance(e, (ConnectionResetError, BrokenPipeError, OSError)):
                 self.log(f"处理 HTTP 请求时出错: {e}")
        finally:
            if remote_socket: remote_socket.close()
            if client_socket: client_socket.close()

    def _handle_socks5_client(self, client_socket):
        """处理SOCKS5客户端请求."""
        remote_socket = None
        try:
            data = client_socket.recv(2)
            if not data or data[0] != 5: return
            nmethods = data[1]
            client_socket.recv(nmethods)
            client_socket.sendall(b"\x05\x00")

            data = client_socket.recv(4)
            if not data or data[0] != 5 or data[1] != 1: return
            
            atyp = data[3]
            if atyp == 1: # IPv4
                addr = socket.inet_ntoa(client_socket.recv(4))
            elif atyp == 3: # Domain
                domain_len = client_socket.recv(1)[0]
                addr = client_socket.recv(domain_len).decode('utf-8')
            else:
                # 暂不支持IPv6
                return
            
            port = struct.unpack('!H', client_socket.recv(2))[0]

            remote_socket = self._get_upstream_connection(addr, port)
            if not remote_socket:
                return

            client_socket.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")

            self._forward_data(client_socket, remote_socket)
        except Exception as e:
            if not isinstance(e, (ConnectionResetError, BrokenPipeError, OSError)):
                self.log(f"处理 SOCKS5 请求时出错: {e}")
        finally:
            if remote_socket: remote_socket.close()
            if client_socket: client_socket.close()

    def _forward_data(self, sock1, sock2):
        """双向转发数据."""
        while self._running:
            try:
                readable, _, exceptional = select.select([sock1, sock2], [], [sock1, sock2], 5)
                if exceptional or not readable:
                    break
                for sock in readable:
                    other_sock = sock2 if sock is sock1 else sock1
                    data = sock.recv(8192)
                    if not data:
                        return
                    other_sock.sendall(data)
            except (ConnectionResetError, BrokenPipeError, OSError, select.error):
                break