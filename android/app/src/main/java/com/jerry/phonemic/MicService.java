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
    private static final int[] CANDIDATE_PORTS = {8080, 8081, 18080, 28080};
    private static final int RATE = 48000;

    private final CopyOnWriteArrayList<OutputStream> clients = new CopyOnWriteArrayList<>();
    private AudioRecord record;
    private android.media.audiofx.NoiseSuppressor ns;
    private volatile boolean running;
    private ServerSocket serverSocket;

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
            android.content.SharedPreferences sp =
                    getSharedPreferences("phonemic", MODE_PRIVATE);
            GAIN_DB = sp.getFloat("gain_db", 0f);
        } catch (Exception ignored) {}
        startForeground(1, buildNotification());

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
                if (n <= 0) continue;
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
            if (ss == null) {   // 全部被占：如实汇报
                RUNNING = false;
                running = false;
                return;
            }
            serverSocket = ss;
            registerNsd(PORT_BOUND);
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
            InputStream in = s.getInputStream();
            int state = 0, b;
            while (state < 4 && (b = in.read()) != -1) {   // 吃掉 HTTP 请求头（到 \r\n\r\n）
                if (b == '\r') state = (state == 0 || state == 2) ? state + 1 : 0;
                else if (b == '\n') state = (state == 1 || state == 3) ? state + 1 : 0;
                else state = 0;
            }
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
