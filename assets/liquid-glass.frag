#version 330
// Polished curved glass: diffuse the transmitted background, keep reflections sharp.
// Canvas ink is a separate premultiplied layer; desktop pixels never enter it.
uniform sampler2D backdrop;
uniform sampler2D softBackdrop;
uniform sampler2D ink;
uniform sampler2D canvasBase;
uniform vec2 resolution;
uniform vec2 textureSize;
uniform vec2 textureOffset;
uniform vec2 pointer;
uniform vec3 tint;
uniform float darkTheme;
uniform float refractionStrength;
uniform int lensCount;
uniform vec4 lenses[4];
uniform vec2 lensShape[4];
out vec4 colorOut;

float boxDistance(vec2 p, vec2 halfSize, float radius) {
    vec2 q = abs(p) - halfSize + radius;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - radius;
}
vec3 background(vec2 p) {
    return texture(backdrop, clamp((p + textureOffset) / textureSize, vec2(.001), vec2(.999))).rgb;
}
vec3 softBackground(vec2 p) {
    return texture(softBackdrop, clamp((p + textureOffset) / textureSize, vec2(.001), vec2(.999))).rgb;
}
vec3 scatter(vec2 p, float spread) {
    vec3 c = background(p) * .4;
    for (int i = 0; i < 8; i++) {
        float a = float(i) * .7853981634;
        c += background(p + vec2(cos(a), sin(a)) * spread) * .075;
    }
    return c;
}
float luminance(vec3 color) {
    vec3 linear = mix(color/12.92,pow((color+.055)/1.055,vec3(2.4)),step(vec3(.04045),color));
    return dot(linear,vec3(.2126,.7152,.0722));
}
float contrast(float a, float b) { return (max(a,b)+.05)/(min(a,b)+.05); }
vec3 lens(vec2 p, vec2 center, vec2 halfSize, float radius, float pressure, float thickness) {
    halfSize *= vec2(1.0 + pressure * .025, 1.0 - pressure * .03);
    radius = min(radius, min(halfSize.x, halfSize.y));
    vec2 local = p - center;
    float d = boxDistance(local, halfSize, radius);
    float bezel = min(25.0, radius * .6);
    float t = clamp(-d / max(bezel, 1.0), .002, 1.0);
    float q = 1.0 - t;
    float profile = max(1.0 - pow(q, 4.0), .00001);
    float height = pow(profile, .25);
    float slope = thickness / max(bezel, 1.0) * pow(q, 3.0) * pow(profile, -.75);
    vec2 gradient = normalize(vec2(
        boxDistance(local + vec2(.4,0), halfSize, radius) - boxDistance(local - vec2(.4,0), halfSize, radius),
        boxDistance(local + vec2(0,.4), halfSize, radius) - boxDistance(local - vec2(0,.4), halfSize, radius)
    ) + vec2(.00001));
    vec3 normal = normalize(vec3(gradient * slope, 1.0));
    vec3 ray = refract(vec3(0,0,-1), normal, 1.0 / 1.46);
    vec2 displacement = ray.xy / max(-ray.z, .05) * height * thickness;
    vec2 sampleAt = center + local / (1.0 + .006 * refractionStrength) + displacement * refractionStrength;
    // Diffusion belongs to the transmitted background, not the surface reflection.
    float clearBevel = 1.0 - smoothstep(.5, 5.5, -d);
    float diffusion = 1.0 - clearBevel * .92;
    vec3 c = mix(scatter(sampleAt, 1.3), softBackground(sampleAt), diffusion);
    float luma = dot(c, vec3(.2126,.7152,.0722));
    c = mix(vec3(luma), c, 1.04);
    c = mix(c, tint, mix(.03, .24, darkTheme));
    vec2 lightPosition = clamp((pointer - center) / resolution, vec2(-.7), vec2(.7));
    vec3 light = normalize(vec3(-.55 + lightPosition.x*.4, -.72 + lightPosition.y*.4, .65));
    float facing = max(dot(gradient, normalize(light.xy)), 0.0);
    // A narrow specular peak with a faint shoulder suggests a smooth surface.
    // Its geometry and directional lighting remain independent of background blur.
    float halfFacing = max(dot(normal, normalize(light + vec3(0,0,1))), 0.0);
    float specular = pow(halfFacing, 112.0) * .86 + pow(halfFacing, 28.0) * .025;
    float rim = exp(-pow((d + .8) / .55, 2.0));
    float reflection = specular + rim * (.045 + pow(facing, 4.0) * .60);
    reflection += pow(1.0-normal.z, 5.0) * (.02 + facing*.08);
    c = mix(c, vec3(1), clamp(reflection, 0.0, .82));
    c *= 1.0-exp(-abs(d+2.2)/1.2)*(1.0-facing)*.09;
    c += exp(-length(p-pointer)/85.0)*pressure*.06;
    return c;
}
void main() {
    vec2 p = vec2(gl_FragCoord.x, resolution.y-gl_FragCoord.y);
    vec2 halfSize = resolution*.5 - vec2(1.0);
    float d = boxDistance(p-resolution*.5, halfSize, 30.0);
    float mask = 1.0-smoothstep(-.65,.65,d);
    if (mask <= 0.0) { colorOut=vec4(0); return; }
    vec3 material = lens(p, resolution*.5, halfSize, 30.0, 0.0, 39.0);
    for (int i=0; i<4; i++) {
        if (i>=lensCount) break;
        vec4 r=lenses[i];
        vec2 center=r.xy+r.zw*.5;
        float distance=boxDistance(p-center, r.zw*.5, lensShape[i].x);
        float a=1.0-smoothstep(-.7,.7,distance);
        if (a>0.0) material=mix(material,lens(p,center,r.zw*.5,lensShape[i].x,lensShape[i].y,44.0),a);
    }
    vec4 base = texture(canvasBase,p/resolution);
    material=base.rgb+material*(1.0-base.a);
    vec4 overlay = texture(ink, p/resolution);
    vec3 foreground=overlay.rgb / max(overlay.a, .0001);
    if (overlay.a>.001) {
        float backgroundLight=luminance(material);
        if (contrast(luminance(foreground),backgroundLight)<4.5) {
            vec3 target=contrast(1.0,backgroundLight)>contrast(0.0,backgroundLight)?vec3(1):vec3(0);
            float low=0.0,high=1.0;
            for (int i=0;i<8;i++) {
                float mid=(low+high)*.5;
                if (contrast(luminance(mix(foreground,target,mid)),backgroundLight)<4.5) low=mid;
                else high=mid;
            }
            float minimumLift=backgroundLight<.08 ? .84 : (backgroundLight>.75 ? .72 : 0.0);
            foreground=mix(foreground,target,max(high,minimumLift));
        }
    }
    vec3 color = foreground*overlay.a + material*(1.0-overlay.a);
    // UpdateLayeredWindow expects premultiplied alpha.
    colorOut=vec4(clamp(color,0.0,1.0)*mask,mask);
}
