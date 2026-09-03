plugins {
    id("com.android.application")
}

android {
    namespace = "com.jerry.phonemic"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.jerry.phonemic"
        minSdk = 26
        targetSdk = 34
        versionCode = 2
        versionName = "1.1"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation("com.google.android.material:material:1.12.0")
}
