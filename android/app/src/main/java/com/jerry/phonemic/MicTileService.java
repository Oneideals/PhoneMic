package com.jerry.phonemic;

import android.Manifest;
import android.app.PendingIntent;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Handler;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;
import android.util.Log;

/** 下拉通知栏快捷磁贴：一键启停麦克风共享，无需解锁进 App。 */
public class MicTileService extends TileService {

    private static final String TAG = "PhoneMic.Tile";
    private final Handler handler = new Handler();

    @Override
    public void onStartListening() {
        Log.d(TAG, "onStartListening");
        update();
        // 修正系统渲染时序导致的磁贴状态滞留
        handler.postDelayed(this::update, 600);
    }

    /** 打开主界面：Android 14+ 禁止磁贴直接 startActivity(Intent)，必须走 PendingIntent。 */
    private void openMainActivity(boolean autostart) {
        Intent m = new Intent(this, MainActivity.class);
        m.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        if (autostart) m.putExtra("autostart", true);
        try {
            if (Build.VERSION.SDK_INT >= 34) {
                PendingIntent pi = PendingIntent.getActivity(this, 0, m,
                        PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
                startActivityAndCollapse(pi);
            } else {
                startActivityAndCollapse(m);
            }
        } catch (Exception e) {
            Log.e(TAG, "打开主界面失败", e);
        }
    }

    @Override
    public void onClick() {
        boolean running = MicService.RUNNING || MicService.PORT_BOUND > 0;
        Log.d(TAG, "onClick: RUNNING=" + MicService.RUNNING
                + " PORT_BOUND=" + MicService.PORT_BOUND
                + " → 执行" + (running ? "停止" : "启动"));
        Intent i = new Intent(this, MicService.class);
        try {
            if (running) {
                stopService(i);
                Log.d(TAG, "stopService 已调用");
            } else if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    == PackageManager.PERMISSION_GRANTED) {
                startForegroundService(i);
                Log.d(TAG, "startForegroundService 已调用");
            } else {
                Log.w(TAG, "无麦克风权限，打开主界面授权");
                openMainActivity(false);
            }
        } catch (Exception e) {
            Log.e(TAG, "磁贴操作异常", e);
        }
        update();
        handler.postDelayed(() -> {
            // Android 14+：若系统拒绝了从磁贴启动（mic FGS 需 eligible 状态），
            // 转跳主界面自动补启动（界面可见即 eligible）
            if (MicService.FGS_BLOCKED) {
                MicService.FGS_BLOCKED = false;
                if (!MicService.RUNNING && MicService.PORT_BOUND <= 0) {
                    openMainActivity(true);
                    return;
                }
            }
            update();
        }, 800);
    }

    private void update() {
        Tile t = getQsTile();
        if (t == null) {
            Log.d(TAG, "update: getQsTile 为 null（尚未绑定）");
            return;
        }
        boolean on = MicService.RUNNING || MicService.PORT_BOUND > 0;
        t.setState(on ? Tile.STATE_ACTIVE : Tile.STATE_INACTIVE);
        t.updateTile();
        Log.d(TAG, "updateTile: " + (on ? "ACTIVE" : "INACTIVE"));
    }
}
