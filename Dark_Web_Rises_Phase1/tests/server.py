import socket

HOST = "127.0.0.1"
PORT = 65432

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
    server_socket.bind((HOST, PORT))

    server_socket.listen()

    conn, addr = server_socket.accept()

    with conn:
        while True:
            data = conn.recv(1024)

            if not data:
                break

            print(f"recieved data: {data.decode('utf-8')}")

            send_data = f"server received:  {data.decode('utf-8')} from conn: {conn}, addr: {addr}".encode('utf-8')

            conn.sendall(send_data)