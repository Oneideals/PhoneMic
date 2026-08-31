package com.jerry.phonemic;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
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
    private static final int[] CANDIDATE_PORTS = {8080, 8081, 18080, 28080};
    private static final int RATE = 48000;
    /** UDP 直连通道：电脑监听 58080 收公告，手机监听 58081 收查询（绕过 mDNS 的兜底） */
    private static final int ANNOUNCE_PORT = 58080;
    private static final int QUERY_PORT = 58081;

    private final CopyOnWriteArrayList<OutputStream> clients = new CopyOnWriteArrayList<>();
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
        for (OutputStream c : clients) { try { c.close(); } catch (Exception ignored) {} }
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

        int minBuf = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT);
        record = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE,
                AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(8192, minBuf * 2));
        record.startRecording();

        // 系统级噪声抑制（过滤电脑风扇等稳态噪声，源头降噪零延迟）
        try {
            if (android.media.audiofx.NoiseSuppressor.isAvailable()) {
                ns = android.media.audiofx.NoiseSuppressor.create(record.getAudioSessionId());
                ns.setEnabled(true);
            }
        } catch (Exception ignored) {}

        Thread micThread = new Thread(() -> {
            byte[] pcm = new byte[4096];
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
                for (OutputStream c : clients) {
                    try {
                        c.write(pcm, 0, n);
                        c.flush();
                    } catch (Exception e) {
                        clients.remove(c);
                        try { c.close(); } catch (Exception ignored) {}
                    }
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
     * UDP 直连通道（mDNS 失效时的兜底发现）：
     * 1) 无客户端连接时每秒广播公告 → 电脑回网后秒级自动重连；
     * 2) 收到电脑 "PHONEMIC_QUERY" 查询立即回公告 → 菜单「立即重连」秒连。
     * （实测：亮屏/息屏均持续广播，前台服务保活了网络栈）
     */
    private void startUdpChannel() {
        new Thread(() -> {
            java.net.DatagramSocket ds = null;
            try {
                ds = new java.net.DatagramSocket(QUERY_PORT);
                ds.setSoTimeout(1000);
                byte[] buf = new byte[64];
                while (running) {
                    try {
                        if (clients.isEmpty() && PORT_BOUND > 0) {
                            byte[] ann = ("PHONEMIC " + PORT_BOUND).getBytes("US-ASCII");
                            ds.send(new java.net.DatagramPacket(ann, ann.length,
                                    java.net.InetAddress.getByName("255.255.255.255"),
                                    ANNOUNCE_PORT));
                        }
                    } catch (Exception ignored) {}
                    try {
                        java.net.DatagramPacket p = new java.net.DatagramPacket(buf, buf.length);
                        ds.receive(p);
                        String msg = new String(p.getData(), 0, p.getLength()).trim();
                        if ("PHONEMIC_QUERY".equals(msg) && PORT_BOUND > 0) {
                            byte[] ann = ("PHONEMIC " + PORT_BOUND).getBytes("US-ASCII");
                            ds.send(new java.net.DatagramPacket(ann, ann.length,
                                    p.getAddress(), ANNOUNCE_PORT));
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

    /** 手动触发：立即连发 3 个公告（主界面「通知电脑」按钮），服务未运行时无效。 */
    public static void announceNow() {
        if (PORT_BOUND <= 0) return;
        new Thread(() -> {
            try {
                java.net.DatagramSocket ds = new java.net.DatagramSocket();
                ds.setBroadcast(true);
                byte[] ann = ("PHONEMIC " + PORT_BOUND).getBytes("US-ASCII");
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

    private void serve(Socket s) {
        OutputStream out = null;
        try {
            s.setTcpNoDelay(true);
            s.setSoTimeout(10000);   // 握手超时：不发请求的连接不会永久占住线程
            InputStream in = s.getInputStream();
            int state = 0, b;
            while (state < 4 && (b = in.read()) != -1) {   // 吃掉 HTTP 请求头（到 \r\n\r\n）
                if (b == '\r') state = (state == 0 || state == 2) ? state + 1 : 0;
                else if (b == '\n') state = (state == 1 || state == 3) ? state + 1 : 0;
                else state = 0;
            }
            s.setSoTimeout(0);       // 流式传输阶段不限时
            out = s.getOutputStream();
            out.write("HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\nConnection: close\r\n\r\n"
                    .getBytes("ISO-8859-1"));
            out.write(WAV_HEADER);
            out.flush();
            clients.add(out);
            while (running && clients.contains(out)) Thread.sleep(1000);
        } catch (Exception ignored) {
        } finally {
            clients.remove(out);
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

    private Notification buildNotification() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel("mic", "麦克风服务",
                NotificationManager.IMPORTANCE_LOW);
        nm.createNotificationChannel(ch);
        return new Notification.Builder(this, "mic")
                .setContentTitle("PhoneMic 运行中")
                .setContentText("麦克风共享中，地址见 App 屏幕")
                .setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .build();
    }
}
