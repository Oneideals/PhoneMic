package com.jerry.phonemic;

import android.app.Application;
import com.google.android.material.color.DynamicColors;

/** 启用 Material You 动态取色：界面主色跟随系统壁纸主题。 */
public class PhonemicApplication extends Application {
    @Override
    public void onCreate() {
        super.onCreate();
        DynamicColors.applyToActivitiesIfAvailable(this);
    }
}
