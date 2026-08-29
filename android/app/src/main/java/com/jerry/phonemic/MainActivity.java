package com.jerry.phonemic;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Color;
import android.os.Bundle;
import android.os.Handler;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import com.google.android.material.button.MaterialButton;
import com.google.android.material.card.MaterialCardView;
import com.google.android.material.color.MaterialColors;
import com.google.android.material.progressindicator.LinearProgressIndicator;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.NetworkInterface;
import java.util.Enumeration;

public class MainActivity extends Activity {

    private static final String PREFS = "phonemic";
    private static final String KEY_GAIN = "gain_db";

    private MaterialButton btnToggle, gainDown, gainUp;
    private TextView statusText, levelText, peakText, gainLabel, addrText;
    private LinearProgressIndicator levelBar;
    private final Handler ticker = new Handler();
    private final Runnable tick = new Runnable() {
        @Override public void run() {
            refresh();
            ticker.postDelayed(this, 300);
        }
    };

    private int dp(float v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    private int themeColor(int attr) {
        return MaterialColors.getColor(findViewById(android.R.id.content), attr);
    }

    private MaterialCardView card(float topMargin) {
        MaterialCardView card = new MaterialCardView(this);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT);
        lp.topMargin = dp(topMargin);
        card.setLayoutParams(lp);
        card.setRadius(dp(16));
        card.setCardElevation(0);
        card.setStrokeWidth(dp(1));
        card.setStrokeColor(MaterialColors.getColor(card,
                com.google.android.material.R.attr.colorOutlineVariant));
        LinearLayout inner = new LinearLayout(this);
        inner.setOrientation(LinearLayout.VERTICAL);
        inner.setPadding(dp(18), dp(16), dp(18), dp(16));
        card.addView(inner);
        card.setTag(inner);
        return card;
    }

    private LinearLayout innerOf(MaterialCardView card) {
        return (LinearLayout) card.getChildAt(0);
    }

    private TextView label(String text, float sp, int color, float bottomDp) {
        TextView tv = new TextView(this);
        tv.setText(text);
        tv.setTextSize(sp);
        tv.setTextColor(color);
        tv.setPadding(0, 0, 0, dp(bottomDp));
        return tv;
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        ScrollView scroll = new ScrollView(this);
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setPadding(dp(20), dp(28), dp(20), dp(32));
        scroll.addView(root);
        setContentView(scroll);

        int onSurface = MaterialColors.getColor(root,
                com.google.android.material.R.attr.colorOnSurface);
        int onSurfVar = MaterialColors.getColor(root,
                com.google.android.material.R.attr.colorOnSurfaceVariant);

        // ── 标题与状态 ──
        TextView title = label("PhoneMic", 26, onSurface, 4);
        title.setTypeface(null, android.graphics.Typeface.BOLD);
        root.addView(title);
        statusText = label("○ 服务未启动", 15, onSurfVar, 12);
        root.addView(statusText);

        // ── 电平卡 ──
        MaterialCardView levelCard = card(4);
        LinearLayout lv = innerOf(levelCard);
        lv.addView(label("输入电平", 14, onSurfVar, 10));
        levelBar = new LinearProgressIndicator(this);
        levelBar.setMax(100);
        levelBar.setTrackThickness(dp(14));
        levelBar.setTrackCornerRadius(dp(7));
        levelBar.setTrackColor(MaterialColors.getColor(levelBar,
                com.google.android.material.R.attr.colorSurfaceContainerHighest));
        levelBar.setIndicatorColor(themeColor(com.google.android.material.R.attr.colorPrimary));
        levelBar.setProgress(0);
        lv.addView(levelBar, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(14)));
        peakText = label("", 14, onSurface, 6);
        peakText.setPadding(0, dp(10), 0, 0);
        lv.addView(peakText);

        // ── 增益行 ──
        LinearLayout gainRow = new LinearLayout(this);
        gainRow.setOrientation(LinearLayout.HORIZONTAL);
        gainRow.setGravity(Gravity.CENTER_VERTICAL);
        gainDown = new MaterialButton(this, null,
                com.google.android.material.R.attr.materialButtonOutlinedStyle);
        gainDown.setText("－");
        gainDown.setOnClickListener(v -> adjustGain(-3));
        gainUp = new MaterialButton(this, null,
                com.google.android.material.R.attr.materialButtonOutlinedStyle);
        gainUp.setText("＋");
        gainUp.setOnClickListener(v -> adjustGain(3));
        gainLabel = new TextView(this);
        gainLabel.setTextSize(18);
        gainLabel.setTextColor(onSurface);
        gainLabel.setGravity(Gravity.CENTER);
        gainLabel.setPadding(dp(24), 0, dp(24), 0);
        LinearLayout.LayoutParams gdl = new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1);
        LinearLayout.LayoutParams gl = new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 2);
        LinearLayout.LayoutParams gur = new LinearLayout.LayoutParams(0,
                LinearLayout.LayoutParams.WRAP_CONTENT, 1);
        gainRow.addView(gainDown, gdl);
        gainRow.addView(gainLabel, gl);
        gainRow.addView(gainUp, gur);
        gainRow.setPadding(0, dp(14), 0, dp(8));
        lv.addView(gainRow);
        lv.addView(label("说话让电平到 40~80%，「削波」不涨即安全。增益管音量，清晰度靠距离。",
                12, onSurfVar, 0));
        root.addView(levelCard);

        // ── 地址卡 ──
        MaterialCardView addrCard = card(16);
        LinearLayout ad = innerOf(addrCard);
        ad.addView(label("电脑连接地址（自动发现，无需手动）", 14, onSurfVar, 8));
        addrText = new TextView(this);
        addrText.setTextSize(14);
        addrText.setTextColor(onSurface);
        addrText.setTextIsSelectable(true);
        ad.addView(addrText);
        root.addView(addrCard);

        // ── 启停大按钮 ──
        btnToggle = new MaterialButton(this);
        btnToggle.setTextSize(17);
        btnToggle.setHeight(dp(56));
        btnToggle.setOnClickListener(v -> toggle());
        LinearLayout.LayoutParams blp = new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT, dp(56));
        blp.topMargin = dp(20);
        root.addView(btnToggle, blp);

        refresh();
    }

    @Override protected void onResume() { super.onResume(); ticker.post(tick); }
    @Override protected void onPause() { super.onPause(); ticker.removeCallbacks(tick); }

    private void adjustGain(int delta) {
        float v = MicService.GAIN_DB + delta;
        if (v < 0) v = 0;
        if (v > 18) v = 18;
        MicService.GAIN_DB = v;
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putFloat(KEY_GAIN, v).apply();
        refresh();
    }

    private void toggle() {
        if (MicService.RUNNING || MicService.PORT_BOUND > 0) {
            stopService(new Intent(this, MicService.class));
        } else {
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{
                        Manifest.permission.RECORD_AUDIO,
                        Manifest.permission.POST_NOTIFICATIONS}, 1);
                return;
            }
            startForegroundService(new Intent(this, MicService.class));
        }
        refresh();
    }

    private void refresh() {
        boolean running = MicService.RUNNING;
        int onSurfVar = MaterialColors.getColor(statusText,
                com.google.android.material.R.attr.colorOnSurfaceVariant);
        int primary = MaterialColors.getColor(statusText,
                com.google.android.material.R.attr.colorPrimary);
        int error = MaterialColors.getColor(statusText,
                com.google.android.material.R.attr.colorError);
        int onPrimary = MaterialColors.getColor(statusText,
                com.google.android.material.R.attr.colorOnPrimary);

        if (running) {
            statusText.setText("● 服务运行中 · 局域网内电脑可自动发现");
            statusText.setTextColor(primary);
        } else {
            statusText.setText("○ 服务未启动");
            statusText.setTextColor(onSurfVar);
        }

        int lv = running ? MicService.LEVEL : 0;
        int hold = MicService.PEAK_HOLD;
        levelBar.setProgress(lv);
        levelBar.setIndicatorColor(lv >= 95 ? error : primary);

        String clipNote = MicService.CLIPS > 0
                ? " · ⚠ 削波 " + MicService.CLIPS
                : " · 削波 0（安全）";
        peakText.setText("峰值保持 " + hold + "%  ·  实时 " + lv + "%" + clipNote);
        peakText.setTextColor(hold >= 95 ? error : onSurfVar);

        gainLabel.setText(String.format("%+ddB", (int) MicService.GAIN_DB));
        addrText.setText(localAddresses(running));

        btnToggle.setText(running ? "停止服务" : "启动麦克风服务");
        btnToggle.setBackgroundTintList(ColorStateList.valueOf(
                running ? onSurfVar : primary));
        btnToggle.setTextColor(running ? MaterialColors.getColor(btnToggle,
                com.google.android.material.R.attr.colorSurface) : onPrimary);
    }

    private String localAddresses(boolean running) {
        StringBuilder sb = new StringBuilder();
        int port = MicService.PORT_BOUND > 0 ? MicService.PORT_BOUND : 8080;
        try {
            Enumeration<NetworkInterface> nis = NetworkInterface.getNetworkInterfaces();
            while (nis.hasMoreElements()) {
                Enumeration<InetAddress> as = nis.nextElement().getInetAddresses();
                while (as.hasMoreElements()) {
                    InetAddress a = as.nextElement();
                    if (a instanceof Inet4Address && !a.isLoopbackAddress() && a.isSiteLocalAddress()) {
                        sb.append("http://").append(a.getHostAddress()).append(":").append(port).append("\n");
                    }
                }
            }
        } catch (Exception ignored) {}
        if (sb.length() == 0) sb.append(running ? "（绑定中…）" : "（启动后自动发现）");
        return sb.toString();
    }
}
