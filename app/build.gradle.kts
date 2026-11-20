plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "uz.xasanboy.optom"
    compileSdk = 34

    defaultConfig {
        applicationId = "uz.xasanboy.optom"
        minSdk = 23
        targetSdk = 34
        versionCode = 4
        versionName = "1.3"
    }

    // Doimiy imzo kaliti — yangi versiyalar eski ilova ustidan (ma'lumotlar saqlangan holda) o'rnatiladi
    signingConfigs {
        create("xo") {
            storeFile = file("xo-key.jks")
            storePassword = "xoptom2026"
            keyAlias = "xo"
            keyPassword = "xoptom2026"
        }
    }

    buildTypes {
        getByName("debug") { signingConfig = signingConfigs.getByName("xo") }
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("xo")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}
