package com.fisheyes;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;

import java.io.BufferedOutputStream;
import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * AstrbotFishEyes 设备端 JPEG 编码器
 *
 * 用法（在手机上通过 app_process 运行，无需安装 APK）：
 *   screencap -p | CLASSPATH=/data/local/tmp/fisheyes_jpeg.dex \
 *       app_process /system/bin com.fisheyes.JpegEncoder <maxWidth> <quality> [pngPath]
 *
 * 参数：
 *   maxWidth  最大宽度（0 = 不缩放）
 *   quality   JPEG 质量 1-100
 *   pngPath   可选：从文件读 PNG；缺省从 stdin 读
 * 输出：
 *   stdout 直接输出 JPEG 字节；失败时 stderr 打印 ERR:xxx 并返回码 1
 */
public final class JpegEncoder {

    public static void main(String[] args) {
        int maxWidth = 0;
        int quality = 70;
        String pngPath = null;
        try {
            if (args.length > 0) {
                maxWidth = Integer.parseInt(args[0].trim());
            }
            if (args.length > 1) {
                quality = Integer.parseInt(args[1].trim());
            }
            if (args.length > 2 && args[2] != null && args[2].trim().length() > 0
                    && !"-".equals(args[2].trim())) {
                pngPath = args[2].trim();
            }
        } catch (NumberFormatException e) {
            System.err.println("ERR:bad_args");
            System.exit(2);
        }
        if (quality < 10) quality = 10;
        if (quality > 100) quality = 100;

        Bitmap src = null;
        Bitmap scaled = null;
        try {
            InputStream in;
            if (pngPath != null) {
                in = new FileInputStream(pngPath);
            } else {
                in = new FileInputStream(FileDescriptor.in);
            }
            src = BitmapFactory.decodeStream(in);
            if (src == null) {
                System.err.println("ERR:decode_null");
                System.exit(3);
            }
            Bitmap out = src;
            int w = src.getWidth();
            int h = src.getHeight();
            if (maxWidth > 0 && w > maxWidth) {
                int nh = Math.max(1, Math.round(h * (float) maxWidth / (float) w));
                scaled = Bitmap.createScaledBitmap(src, maxWidth, nh, true);
                out = scaled;
            }
            OutputStream os = new BufferedOutputStream(new FileOutputStream(FileDescriptor.out), 65536);
            boolean ok = out.compress(Bitmap.CompressFormat.JPEG, quality, os);
            os.flush();
            if (!ok) {
                System.err.println("ERR:compress_false");
                System.exit(4);
            }
        } catch (Throwable t) {
            System.err.println("ERR:" + t.getClass().getSimpleName() + ":" + t.getMessage());
            System.exit(5);
        } finally {
            try {
                if (scaled != null && scaled != src) scaled.recycle();
                if (src != null) src.recycle();
            } catch (Throwable ignored) {
            }
        }
    }
}
