#version 330
// Separable Gaussian over the background only, before the material and UI.
uniform sampler2D sourceImage;
uniform vec2 targetSize;
uniform vec2 stepUV;
uniform float sigma;
out vec4 colorOut;

void main() {
    vec2 uv = gl_FragCoord.xy / targetSize;
    if (sigma <= 0.0) { colorOut = vec4(texture(sourceImage, uv).rgb, 1); return; }
    vec3 total = vec3(0);
    float weightSum = 0.0;
    for (int i = -12; i <= 12; i++) {
        // Cover three sigmas at larger user settings/DPI without extra taps.
        float offset = float(i) * max(1.0, sigma / 4.0);
        float weight = exp(-.5 * pow(offset / sigma, 2.0));
        total += texture(sourceImage, uv + stepUV * offset).rgb * weight;
        weightSum += weight;
    }
    colorOut = vec4(total / weightSum, 1);
}
