import socket

HOST = "127.0.0.1"
PORT = 65432

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
    client_socket.connect((HOST, PORT))

    message = "test".encode('utf=8')

    client_socket.sendall(message)
    
    data = client_socket.recv(1024)

    print(f"recieved:  {data.decode('utf-8')} from server")