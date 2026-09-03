package com.jerry.phonemic;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.os.IBinder;
import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.concurrent.CopyOnWriteArrayList;

/** 把麦克风做成无限 WAV 流，HTTP 服务自动挑选可用端口。 */
public class MicService extends Service {

    public static volatile boolean RUNNING = false;
    public static volatile int PORT_BOUND = -1;
    /** 麦克风数字增益（dB，0~18），主界面可实时调整 */
    public static volatile float GAIN_DB = 0f;
    /** 实时输入电平 0~100，供主界面电平条显示 */
    public static volatile int LEVEL = 0;
    /** 3 秒峰值保持（说话时看它判断增益是否合适） */
    public static volatile int PEAK_HOLD = 0;
    private static volatile long PEAK_HOLD_AT = 0;
    /** 累计削波采样数：不增长 = 增益安全 */
    public static volatile int CLIPS = 0;
    /** Android 14+ 从非 eligible 状态（如磁贴）启动 mic FGS 被系统拒绝时置位，磁贴据此转跳主界面 */
    public static volatile boolean FGS_BLOCKED = false;
    public static volatile boolean HAS_USB_LINK = false;
    /** 麦克风打开失败的原因（null=正常），主界面据此如实显示而不是假装在跑 */
    public static volatile String MIC_ERROR = null;

    private static boolean hasUsbClient() {
        for (Client c : clients) if (c.usb && c.alive) return true;
        return false;
    }

    public static String getActiveLinkMode() {
        if (!RUNNING) return "○ 服务未启动";
        if (HAS_USB_LINK || hasUsbClient()) return "⚡ USB 物理直连 (<1ms 极速)";
        if (!udpClients.isEmpty()) return "📡 UDP 极速无线流 (15ms 低延迟)";
        if (!clients.isEmpty()) return "📶 Wi-Fi 局域网传输中 (40ms)";
        return "⏸ 待机中（等待电脑连接，USB / Wi-Fi 已就绪）";
    }

    public static String getActiveLinkDetail() {
        if (!RUNNING) return "请点击下方「启动麦克风服务」";
        if (HAS_USB_LINK || hasUsbClient()) return "已通过 USB 数据线直连电脑，延迟 <1ms，零网络抖动";
        if (!udpClients.isEmpty()) return "已建立 UDP 10ms 极速无线推流，无 TCP 队头阻塞";
        if (!clients.isEmpty()) return "正在通过 Wi-Fi 局域网传输音频流";
        return "电脑端打开 PhoneMic 即可自动秒连（插线走 USB，拔线走 UDP）";
    }

    /** 读取（必要时生成）配对码：无线连接凭它鉴权，回环(USB) 连接免鉴权且可取回它。 */
    public static String ensureToken(android.content.Context ctx) {
        android.content.SharedPreferences sp =
                ctx.getSharedPreferences(PREFS, android.content.Context.MODE_PRIVATE);
        String t = sp.getString(KEY_TOKEN, "");
        if (t == null || t.isEmpty()) {
            byte[] b = new byte[9];
            new java.security.SecureRandom().nextBytes(b);
            t = android.util.Base64.encodeToString(b, android.util.Base64.NO_PADDING
                    | android.util.Base64.NO_WRAP | android.util.Base64.URL_SAFE);
            sp.edit().putString(KEY_TOKEN, t).apply();
        }
        TOKEN = t;
        return t;
    }

    private static volatile String TOKEN = "";

    private static final int[] CANDIDATE_PORTS = {8080, 8081, 18080, 28080};
    private static final int RATE = 48000;
    private static final int FRAME_BYTES = 960;   // 480 采样 = 10.0ms
    /** 每客户端队列深度：64 帧 ≈ 640ms，够扛 Wi-Fi 抖动又不会积压出可闻延迟 */
    private static final int QUEUE_FRAMES = 64;
    /** UDP 直连通道：电脑监听 58080 收公告，手机监听 58081 收查询（绕过 mDNS 的兜底） */
    private static final int ANNOUNCE_PORT = 58080;
    private static final int QUERY_PORT = 58081;
    public static final int UDP_AUDIO_PORT = 58082;
    private static final String PREFS = "phonemic";
    private static final String KEY_TOKEN = "pair_token";

    /**
     * 一个已连接的 HTTP 客户端：有界队列 + 独立写线程。
     *
     * 采集线程此前对每个客户端做**同步阻塞** write：任一客户端读得慢（Wi-Fi 拥塞，
     * 或恶意客户端连上就是不读）都会卡住整个采集循环，`record.read()` 不被调用，
     * AudioRecord 环形缓冲溢出，于是**所有**客户端一起丢音。
     * 现在慢客户端只丢自己队列里的帧，采集线程永不阻塞。
     */
    private static final class Client {
        final Socket sock;
        final OutputStream out;
        final boolean usb;
        final java.util.concurrent.ArrayBlockingQueue<byte[]> q =
                new java.util.concurrent.ArrayBlockingQueue<>(QUEUE_FRAMES);
        volatile boolean alive = true;
        volatile int dropped = 0;

        Client(Socket sock, OutputStream out, boolean usb) {
            this.sock = sock;
            this.out = out;
            this.usb = usb;
        }

        /** 采集线程调用：永不阻塞，队列满就丢最旧的一帧。 */
        void offer(byte[] frame) {
            if (!alive) return;
            while (!q.offer(frame)) {
                if (q.poll() == null) return;
                dropped++;
            }
        }

        /** 独立写线程：阻塞只发生在这里。 */
        void pump() {
            try {
                while (alive && RUNNING) {
                    byte[] f = q.poll(1, java.util.concurrent.TimeUnit.SECONDS);
                    if (f == null) continue;
                    out.write(f);
                    out.flush();
                }
            } catch (Exception ignored) {
            } finally {
                alive = false;
            }
        }

        void close() {
            alive = false;
            try { sock.close(); } catch (Exception ignored) {}
        }
    }

    private static final CopyOnWriteArrayList<Client> clients = new CopyOnWriteArrayList<>();
    private static final java.util.concurrent.ConcurrentHashMap<java.net.InetSocketAddress, Long> udpClients =
            new java.util.concurrent.ConcurrentHashMap<>();
    private java.net.DatagramSocket udpAudioSocket;
    private int udpSeq = 0;
    private AudioRecord record;
    private android.media.audiofx.NoiseSuppressor ns;
    private volatile boolean running;
    private ServerSocket serverSocket;
    private android.os.PowerManager.WakeLock wakeLock;
    private android.net.wifi.WifiManager.WifiLock wifiLock;

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        startInternal();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        android.util.Log.d("PhoneMic.Svc", "onDestroy: 服务停止");
        running = false;
        RUNNING = false;
        PORT_BOUND = -1;
        unregisterNsd();
        try { if (wakeLock != null && wakeLock.isHeld()) wakeLock.release(); } catch (Exception ignored) {}
        try { if (wifiLock != null && wifiLock.isHeld()) wifiLock.release(); } catch (Exception ignored) {}
        try { if (record != null) { record.stop(); record.release(); } } catch (Exception ignored) {}
        try { if (ns != null) ns.release(); } catch (Exception ignored) {}
        try { if (serverSocket != null) serverSocket.close(); } catch (Exception ignored) {}
        try { if (udpAudioSocket != null) udpAudioSocket.close(); } catch (Exception ignored) {}
        udpClients.clear();
        HAS_USB_LINK = false;
        for (Client c : clients) c.close();
        clients.clear();
        super.onDestroy();
    }

    private synchronized void startInternal() {
        if (running) return;
        running = true;
        RUNNING = true;
        android.util.Log.d("PhoneMic.Svc", "startInternal: 服务启动");
        try {
            android.os.PowerManager pm = (android.os.PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, "PhoneMic:WakeLock");
                wakeLock.acquire();
            }
        } catch (Exception e) {
            android.util.Log.w("PhoneMic.Svc", "获取 WakeLock 失败: " + e);
        }

        try {
            android.net.wifi.WifiManager wm = (android.net.wifi.WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            if (wm != null) {
                int mode = android.net.wifi.WifiManager.WIFI_MODE_FULL_HIGH_PERF;
                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.Q) {
                    mode = android.net.wifi.WifiManager.WIFI_MODE_FULL_LOW_LATENCY;
                }
                wifiLock = wm.createWifiLock(mode, "PhoneMic:WifiLock");
                wifiLock.acquire();
            }
        } catch (Exception e) {
            android.util.Log.w("PhoneMic.Svc", "获取 WifiLock 失败: " + e);
        }

        try {
            android.content.SharedPreferences sp =
                    getSharedPreferences("phonemic", MODE_PRIVATE);
            GAIN_DB = sp.getFloat("gain_db", 0f);
        } catch (Exception ignored) {}
        try {
            startForeground(1, buildNotification());
        } catch (Exception e) {
            // Android 14+：麦克风类型 FGS 只能在 eligible 状态（如界面可见）启动，
            // 从磁贴等后台入口启动会被系统拒绝。不能崩溃循环，置标志让磁贴转跳主界面补启动。
            android.util.Log.w("PhoneMic.Svc", "startForeground 被拒绝（非 eligible 状态）: " + e);
            running = false;
            RUNNING = false;
            FGS_BLOCKED = true;
            stopSelf();
            return;
        }

        try {
            udpAudioSocket = new java.net.DatagramSocket(UDP_AUDIO_PORT);
        } catch (Exception e) {
            try {
                udpAudioSocket = new java.net.DatagramSocket();
            } catch (Exception ignored) {}
        }

        ensureToken(this);

        int minBuf = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        // 权限被运行时撤销、或麦克风被其他 App 独占时，构造/startRecording 会抛异常。
        // 不接住的话异常会从 onStartCommand 逃逸把 App 打崩，而且此时 RUNNING 已置位、
        // 前台通知已挂出，留下一个"看起来在跑其实没跑"的僵尸状态。
        try {
            record = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                    Math.max(8192, minBuf * 2));
            if (record.getState() != AudioRecord.STATE_INITIALIZED) {
                throw new IllegalStateException("AudioRecord 未初始化（麦克风权限或被占用）");
            }
            record.startRecording();
        } catch (Exception e) {
            android.util.Log.e("PhoneMic.Svc", "麦克风初始化失败", e);
            try { if (record != null) record.release(); } catch (Exception ignored) {}
            record = null;
            MIC_ERROR = "麦克风打开失败：" + e.getMessage();
            running = false;
            RUNNING = false;
            stopForeground(true);
            stopSelf();
            return;
        }
        MIC_ERROR = null;

        // 系统级噪声抑制（过滤电脑风扇等稳态噪声，源头降噪零延迟）
        try {
            if (android.media.audiofx.NoiseSuppressor.isAvailable()) {
                ns = android.media.audiofx.NoiseSuppressor.create(record.getAudioSessionId());
                ns.setEnabled(true);
            }
        } catch (Exception ignored) {}

        Thread micThread = new Thread(() -> {
            byte[] pcm = new byte[FRAME_BYTES];      // 480 采样 = 10.0 ms 极速帧
            byte[] udpPacket = new byte[8 + FRAME_BYTES];
            byte[] pending = new byte[FRAME_BYTES * 2];   // 短读残留，攒够一帧再发 UDP
            int pendingLen = 0;
            udpPacket[0] = 'P'; udpPacket[1] = 'M'; udpPacket[2] = 'I'; udpPacket[3] = 'C';
            while (running) {
                int n = record.read(pcm, 0, pcm.length);
                if (n < 0) {
                    // 读取错误（麦克风被占用/初始化失败）：睡眠降频，避免空转烧 CPU
                    if (!running) break;
                    try { Thread.sleep(50); } catch (InterruptedException e) { break; }
                    continue;
                }
                if (n == 0) continue;
                // 数字增益（16bit 小端，两字节一个采样），带削波保护 + 电平统计
                float g = (float) Math.pow(10, GAIN_DB / 20.0);
                int peak = 0, clippedNow = 0;
                for (int i = 0; i + 1 < n; i += 2) {
                    short v = (short) ((pcm[i] & 0xFF) | (pcm[i + 1] << 8));
                    int a = (int) (v * g);
                    if (a > 32767) { a = 32767; clippedNow++; }
                    else if (a < -32768) { a = -32768; clippedNow++; }
                    pcm[i] = (byte) a;
                    pcm[i + 1] = (byte) (a >> 8);
                    int abs = a < 0 ? -a : a;
                    if (abs > peak) peak = abs;
                }
                if (clippedNow > 0) CLIPS += clippedNow;
                LEVEL = Math.max(peak * 100 / 32768, (LEVEL * 90) / 100);
                long nowMs = android.os.SystemClock.elapsedRealtime();
                int pct = peak * 100 / 32768;
                if (pct >= PEAK_HOLD || nowMs - PEAK_HOLD_AT > 3000) {
                    PEAK_HOLD = pct;
                    PEAK_HOLD_AT = nowMs;
                }
                // 1. 投递给 HTTP TCP 客户端（入队即返回，绝不在采集线程上阻塞）
                if (!clients.isEmpty()) {
                    byte[] frame = java.util.Arrays.copyOf(pcm, n);
                    for (Client c : clients) {
                        if (c.alive) c.offer(frame);
                        else clients.remove(c);
                    }
                }
                // 2. 发送 UDP 实时推流客户端（10ms 帧，带 PMIC 头与序列号）。
                //    record.read 可能返回不足一帧，先攒够 FRAME_BYTES 再发，
                //    否则短读那一帧会被整帧丢掉（UDP 侧静默丢音）。
                if (udpAudioSocket != null && !udpClients.isEmpty()) {
                    System.arraycopy(pcm, 0, pending, pendingLen, n);
                    pendingLen += n;
                    while (pendingLen >= FRAME_BYTES) {
                        udpPacket[4] = (byte) (udpSeq >> 24);
                        udpPacket[5] = (byte) (udpSeq >> 16);
                        udpPacket[6] = (byte) (udpSeq >> 8);
                        udpPacket[7] = (byte) (udpSeq);
                        udpSeq++;
                        System.arraycopy(pending, 0, udpPacket, 8, FRAME_BYTES);
                        pendingLen -= FRAME_BYTES;
                        System.arraycopy(pending, FRAME_BYTES, pending, 0, pendingLen);
                        long nowWall = System.currentTimeMillis();
                        for (java.util.Map.Entry<java.net.InetSocketAddress, Long> entry : udpClients.entrySet()) {
                            if (nowWall - entry.getValue() > 8000) {
                                udpClients.remove(entry.getKey());
                                if (clients.isEmpty() && udpClients.isEmpty()) {
                                    onClientDisconnected();
                                }
                                continue;
                            }
                            try {
                                java.net.DatagramPacket dp = new java.net.DatagramPacket(
                                        udpPacket, udpPacket.length, entry.getKey());
                                udpAudioSocket.send(dp);
                            } catch (Exception ignored) {}
                        }
                    }
                } else {
                    pendingLen = 0;
                }
            }
        }, "mic");
        micThread.start();

        Thread serverThread = new Thread(() -> {
            ServerSocket ss = null;
            for (int p : CANDIDATE_PORTS) {
                try {
                    ss = new ServerSocket(p);
                    PORT_BOUND = p;
                    break;
                } catch (Exception e) {
                    // 端口被占，试下一个
                }
            }
            if (ss == null) {   // 全部被占：如实汇报并结束服务（避免残留空转的前台通知）
                RUNNING = false;
                running = false;
                stopSelf();
                return;
            }
            serverSocket = ss;
            registerNsd(PORT_BOUND);
            startUdpChannel();
            try {
                while (running) {
                    final Socket s = ss.accept();
                    new Thread(() -> serve(s)).start();
                }
            } catch (Exception ignored) {
            }
        }, "server");
        serverThread.start();
    }

    /**
     * UDP 直连通道（mDNS 失效时的兜底发现与 UDP 推流注册）：
     * 1) 周期广播公告（含 TCP 端口与 UDP 音频端口）；
     * 2) 接收 "PHONEMIC_UDP_START" / "PHONEMIC_UDP_PING" 注册客户端；
     * 3) 收到 "PHONEMIC_QUERY" 查询立即回公告。
     */
    /** 公告报文：'PHONEMIC <tcp> UDP <udp> RATE <hz>'，电脑端 parse_announce 解析。 */
    private static byte[] announcement() throws Exception {
        return ("PHONEMIC " + PORT_BOUND + " UDP " + UDP_AUDIO_PORT + " RATE " + RATE)
                .getBytes("US-ASCII");
    }

    private void startUdpChannel() {
        new Thread(() -> {
            java.net.DatagramSocket ds = null;
            try {
                ds = new java.net.DatagramSocket(QUERY_PORT);
                ds.setBroadcast(true);   // 与 announceNow() 保持一致，别指望默认值
                ds.setSoTimeout(1000);
                byte[] buf = new byte[128];
                while (running) {
                    try {
                        if (clients.isEmpty() && udpClients.isEmpty() && PORT_BOUND > 0) {
                            byte[] ann = announcement();
                            ds.send(new java.net.DatagramPacket(ann, ann.length,
                                    java.net.InetAddress.getByName("255.255.255.255"),
                                    ANNOUNCE_PORT));
                        }
                    } catch (Exception ignored) {}
                    try {
                        java.net.DatagramPacket p = new java.net.DatagramPacket(buf, buf.length);
                        ds.receive(p);
                        String msg = new String(p.getData(), 0, p.getLength()).trim();
                        java.net.InetSocketAddress from = (java.net.InetSocketAddress) p.getSocketAddress();
                        if ("PHONEMIC_QUERY".equals(msg) && PORT_BOUND > 0) {
                            // 公告只含 IP/端口，不含音频，可以无条件应答
                            byte[] ann = announcement();
                            ds.send(new java.net.DatagramPacket(ann, ann.length,
                                    p.getAddress(), ANNOUNCE_PORT));
                        } else if (msg.startsWith("PHONEMIC_UDP_START")
                                || msg.startsWith("PHONEMIC_UDP_PING")) {
                            // 必须验配对码：此前任何一个 UDP 包都能让手机朝包里的源地址
                            // 持续推 8 秒实时录音 —— 源地址还能伪造，既是窃听也是放大反射源
                            String given = msg.contains(" ")
                                    ? msg.substring(msg.indexOf(' ') + 1).trim() : "";
                            if (tokenOk(given)) {
                                boolean wasEmpty = clients.isEmpty() && udpClients.isEmpty();
                                udpClients.put(from, System.currentTimeMillis());
                                if (wasEmpty) onClientConnected(false);
                            } else if (udpRejectLog++ % 20 == 0) {
                                android.util.Log.w("PhoneMic.Svc",
                                        "拒绝未配对的 UDP 注册: " + from);
                            }
                        } else if (msg.startsWith("PHONEMIC_UDP_STOP")) {
                            udpClients.remove(from);
                            if (clients.isEmpty() && udpClients.isEmpty()) {
                                onClientDisconnected();
                            }
                        }
                    } catch (java.net.SocketTimeoutException ignored) {
                    } catch (Exception ignored) {}
                }
            } catch (Exception ignored) {
            } finally {
                try { if (ds != null) ds.close(); } catch (Exception ignored) {}
            }
        }, "udp").start();
    }

    private int udpRejectLog = 0;

    /** 手动触发：立即连发 3 个公告（主界面「通知电脑」按钮），服务未运行时无效。 */
    public static void announceNow() {
        if (PORT_BOUND <= 0) return;
        new Thread(() -> {
            try {
                java.net.DatagramSocket ds = new java.net.DatagramSocket();
                ds.setBroadcast(true);
                byte[] ann = announcement();
                for (int i = 0; i < 3; i++) {
                    ds.send(new java.net.DatagramPacket(ann, ann.length,
                            java.net.InetAddress.getByName("255.255.255.255"), ANNOUNCE_PORT));
                    Thread.sleep(150);
                }
                ds.close();
            } catch (Exception ignored) {}
        }).start();
    }

    private NsdManager.RegistrationListener nsdListener;

    /** 向局域网广播 _phonemic._tcp 服务，电脑端可自动发现，无需记住端口。 */
    private void registerNsd(int port) {
        try {
            NsdServiceInfo info = new NsdServiceInfo();
            info.setServiceName("PhoneMic");
            info.setServiceType("_phonemic._tcp.");
            info.setPort(port);
            nsdListener = new NsdManager.RegistrationListener() {
                @Override public void onServiceRegistered(NsdServiceInfo i) {}
                @Override public void onRegistrationFailed(NsdServiceInfo i, int e) {}
                @Override public void onServiceUnregistered(NsdServiceInfo i) {}
                @Override public void onUnregistrationFailed(NsdServiceInfo i, int e) {}
            };
            NsdManager nm = (NsdManager) getSystemService(NSD_SERVICE);
            nm.registerService(info, NsdManager.PROTOCOL_DNS_SD, nsdListener);
        } catch (Exception ignored) {
        }
    }

    private void unregisterNsd() {
        try {
            if (nsdListener != null) {
                ((NsdManager) getSystemService(NSD_SERVICE)).unregisterService(nsdListener);
                nsdListener = null;
            }
        } catch (Exception ignored) {
        }
    }

    /** 从请求头里取配对码：优先 X-PhoneMic-Token，其次 URL 上的 ?t= 。 */
    private static String extractToken(String req) {
        for (String line : req.split("\r\n")) {
            if (line.toLowerCase(java.util.Locale.ROOT).startsWith("x-phonemic-token:")) {
                return line.substring(line.indexOf(':') + 1).trim();
            }
        }
        int q = req.indexOf("?t=");
        if (q >= 0) {
            int end = q + 3;
            while (end < req.length() && " \r\n&".indexOf(req.charAt(end)) < 0) end++;
            return req.substring(q + 3, end);
        }
        return "";
    }

    private static boolean tokenOk(String given) {
        String want = TOKEN;
        if (want == null || want.isEmpty()) return true;   // 尚未生成配对码：不锁死用户
        return java.security.MessageDigest.isEqual(
                given.getBytes(java.nio.charset.StandardCharsets.UTF_8),
                want.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }

    private static void writeSimple(OutputStream out, String status, String body) throws Exception {
        byte[] b = body.getBytes("UTF-8");
        out.write(("HTTP/1.1 " + status + "\r\nContent-Type: text/plain; charset=utf-8\r\n"
                + "Content-Length: " + b.length + "\r\nConnection: close\r\n\r\n")
                .getBytes("ISO-8859-1"));
        out.write(b);
        out.flush();
    }

    private void serve(Socket s) {
        Client client = null;
        try {
            String remote = s.getInetAddress() != null ? s.getInetAddress().getHostAddress() : "";
            boolean isUsb = s.getInetAddress() != null && (s.getInetAddress().isLoopbackAddress()
                    || remote.contains("127.0.0.1") || remote.equals("::1") || "localhost".equalsIgnoreCase(remote));
            android.util.Log.i("PhoneMic.Svc", ">>> serve: remote=" + remote + " isUsb=" + isUsb);
            s.setTcpNoDelay(true);
            s.setSoTimeout(10000);   // 握手超时：不发请求的连接不会永久占住线程
            InputStream in = s.getInputStream();
            StringBuilder head = new StringBuilder();
            int state = 0, b;
            while (state < 4 && head.length() < 8192 && (b = in.read()) != -1) {
                head.append((char) b);
                if (b == '\r') state = (state == 0 || state == 2) ? state + 1 : 0;
                else if (b == '\n') state = (state == 1 || state == 3) ? state + 1 : 0;
                else state = 0;
            }
            String req = head.toString();
            OutputStream out = s.getOutputStream();

            // USB(回环) 通道免鉴权：电脑经 adb forward 连上时可直接取回配对码，
            // 此后无线连接凭它鉴权 —— 插过线的用户全程无感，无需手抄。
            if (req.startsWith("GET /token")) {
                if (!isUsb) {
                    writeSimple(out, "403 Forbidden", "pairing only over USB\n");
                } else {
                    writeSimple(out, "200 OK", TOKEN);
                }
                return;
            }
            // 无线连接必须带配对码：否则同一 Wi-Fi 下任何人 curl 一下就能听你的麦克风
            if (!isUsb && !tokenOk(extractToken(req))) {
                android.util.Log.w("PhoneMic.Svc", "拒绝未配对的连接: " + remote);
                writeSimple(out, "401 Unauthorized", "pair first (see PhoneMic app)\n");
                return;
            }

            s.setSoTimeout(0);       // 流式传输阶段不限时
            out.write("HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nConnection: close\r\n\r\n"
                    .getBytes("ISO-8859-1"));
            out.write(WAV_HEADER);
            out.flush();
            boolean wasEmpty = clients.isEmpty() && udpClients.isEmpty();
            client = new Client(s, out, isUsb);
            clients.add(client);
            if (isUsb) HAS_USB_LINK = true;
            if (wasEmpty) onClientConnected(isUsb);
            client.pump();           // 阻塞在本客户端自己的线程上，与采集线程无关
        } catch (Exception ignored) {
        } finally {
            if (client != null) {
                clients.remove(client);
                if (client.dropped > 0) {
                    android.util.Log.w("PhoneMic.Svc", "客户端断开，累计丢帧 " + client.dropped);
                }
                if (client.usb) HAS_USB_LINK = hasUsbClient();
                if (clients.isEmpty() && udpClients.isEmpty()) {
                    onClientDisconnected();
                }
            }
            try { s.close(); } catch (Exception ignored) {}
        }
    }

    private static final byte[] WAV_HEADER = buildHeader();

    private static byte[] buildHeader() {
        int dataLen = 0x7FFFFF00;   // 假的巨大长度：无限流
        ByteArrayOutputStream o = new ByteArrayOutputStream(44);
        DataOutputStream d = new DataOutputStream(o);
        try {
            d.writeBytes("RIFF"); d.writeInt(Integer.reverseBytes(36 + dataLen)); d.writeBytes("WAVE");
            d.writeBytes("fmt "); d.writeInt(Integer.reverseBytes(16));
            d.writeShort(Short.reverseBytes((short) 1));           // PCM
            d.writeShort(Short.reverseBytes((short) 1));           // 单声道
            d.writeInt(Integer.reverseBytes(RATE));
            d.writeInt(Integer.reverseBytes(RATE * 2));            // 字节率
            d.writeShort(Short.reverseBytes((short) 2));           // 块对齐
            d.writeShort(Short.reverseBytes((short) 16));          // 16bit
            d.writeBytes("data"); d.writeInt(Integer.reverseBytes(dataLen));
        } catch (Exception e) {
            throw new RuntimeException(e);
        }
        return o.toByteArray();
    }

    private void onClientConnected(boolean isUsb) {
        try {
            // 1. 震动反馈 (轻快连击 2 下)
            android.os.Vibrator v = (android.os.Vibrator) getSystemService(VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) {
                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                    v.vibrate(android.os.VibrationEffect.createWaveform(new long[]{0, 50, 70, 70}, -1));
                } else {
                    v.vibrate(new long[]{0, 50, 70, 70}, -1);
                }
            }
            // 2. 播放系统提示音
            android.net.Uri sound = android.media.RingtoneManager.getDefaultUri(android.media.RingtoneManager.TYPE_NOTIFICATION);
            if (sound != null) {
                android.media.Ringtone r = android.media.RingtoneManager.getRingtone(getApplicationContext(), sound);
                if (r != null) r.play();
            }
            // 3. 更新前台通知
            updateNotification("PhoneMic 已连通", isUsb ? "⚡ USB 物理直连推流中" : "📡 无线推流中");
        } catch (Exception ignored) {}
    }

    private void onClientDisconnected() {
        try {
            // 1. 震动反馈 (明显警示长震 250ms)
            android.os.Vibrator v = (android.os.Vibrator) getSystemService(VIBRATOR_SERVICE);
            if (v != null && v.hasVibrator()) {
                if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
                    v.vibrate(android.os.VibrationEffect.createOneShot(250, android.os.VibrationEffect.DEFAULT_AMPLITUDE));
                } else {
                    v.vibrate(250);
                }
            }
            // 2. 播放系统提示音
            android.net.Uri sound = android.media.RingtoneManager.getDefaultUri(android.media.RingtoneManager.TYPE_NOTIFICATION);
            if (sound != null) {
                android.media.Ringtone r = android.media.RingtoneManager.getRingtone(getApplicationContext(), sound);
                if (r != null) r.play();
            }
            // 3. 更新前台通知
            updateNotification("PhoneMic 待机中", "电脑已断开连接，等待重新连接");
        } catch (Exception ignored) {}
    }

    private PendingIntent getNotificationPendingIntent() {
        Intent intent = new Intent(this, MainActivity.class);
        intent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        return PendingIntent.getActivity(
                this, 0, intent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private void updateNotification(String title, String content) {
        try {
            NotificationManager nm = getSystemService(NotificationManager.class);
            if (nm != null) {
                Notification n = new Notification.Builder(this, "mic")
                        .setContentTitle(title)
                        .setContentText(content)
                        .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                        .setContentIntent(getNotificationPendingIntent())
                        .setOngoing(true)
                        .build();
                nm.notify(1, n);
            }
        } catch (Exception ignored) {}
    }

    private Notification buildNotification() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel("mic", "麦克风服务",
                NotificationManager.IMPORTANCE_LOW);
        nm.createNotificationChannel(ch);
        return new Notification.Builder(this, "mic")
                .setContentTitle("PhoneMic 待机中")
                .setContentText("等待电脑连接，地址见 App 屏幕")
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentIntent(getNotificationPendingIntent())
                .build();
    }
}
