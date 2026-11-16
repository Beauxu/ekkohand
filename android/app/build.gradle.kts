plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "cv.backpeng.ekkohand"
    compileSdk = 34

    defaultConfig {
        applicationId = "cv.backpeng.ekkohand"
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "1.0.0"
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
    kotlinOptions {
        jvmTarget = "17"
    }
    // 不引第三方依赖：HTTP 用 HttpURLConnection，JSON 用 org.json
}
