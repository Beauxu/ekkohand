plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// release 签名：CI 里由 secrets 解码出 .p12 并设置 ANDROID_KEYSTORE_PATH。
// 本地没有 key 时自动退回 debug 签名，不会让构建失败。
val ksPath = System.getenv("ANDROID_KEYSTORE_PATH")
val ksFile = ksPath?.let { file(it) }
val hasReleaseKey = ksFile != null && ksFile.exists()

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

    signingConfigs {
        if (hasReleaseKey) {
            create("release") {
                storeFile = ksFile
                storeType = "PKCS12"
                storePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ANDROID_KEY_ALIAS") ?: "ekkohand"
                keyPassword = System.getenv("ANDROID_KEY_PASSWORD")
                    ?: System.getenv("ANDROID_KEYSTORE_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (hasReleaseKey) {
                signingConfig = signingConfigs.getByName("release")
            }
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
