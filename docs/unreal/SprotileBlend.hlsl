// Sprotile blend decoder (Sprotile 5.14, UE 4.27). Mirrors Sprotile's shared-blend shader in Blender.
// Inputs (5.13 layout): UV0 and UVA both TexCoord 0 (the render map: the base tile, and tile A on blended faces),
// UVB TexCoord 1, Factor TexCoord 2 (x = gradient position or overlay opacity, y = mode),
// Edge TexCoord 3 (x = tuft phase, y = tuft amount; grass > 0, snow < 0). Sprotile 5.12 used TexCoords 0, 1, 2, 4, 5.
// Unreal's FBX import flips V (v -> 1 - v), so the mode and the edge amount are read as 1 - y.
// Modes: 0 plain tile, 1 linear blend, 2 smooth blend, 3+ overlay (3 = Overlay 1, 4 = Overlay 2, ...).
// Paths: 1.05..1.45 (linear) or 2.05..2.45 (smooth). x is then the signed distance from the path's centre in
// outer half-widths, and frac(mode) says where B is fully on: inner = frac * 5 - 1.25 (Sprotile 5.11 paths).
// New Custom-node inputs since 5.14: VertexAlpha (VertexColor node's A output) and AlphaStrength (scalar parameter,
// default 0). Both can be left unconnected on a master that predates 5.14 if you delete the line that uses them.
float4 baseC = Texture2DSample(BaseTex, BaseTexSampler, UV0);
float2 gxA = ddx(UVA), gyA = ddy(UVA);
float2 gxB = ddx(UVB), gyB = ddy(UVB);
float mode = 1.0 - Factor.y;
// A mesh without Sprotile blend maps gets its last UV channel repeated into TexCoords 1-3, so Factor == UVB means
// a plain mesh: it shows the base tile, exactly like qtderg's original master. One master serves every level mesh.
if (Enable < 0.5 || mode < 0.5 || all(Factor == UVB)) return baseC;
float4 a = Texture2DSampleGrad(BaseTex, BaseTexSampler, UVA, gxA, gyA);
if (mode > 2.5)
{
    float slot = round(mode) - 2.0;
    float4 o;
    if (slot < 1.5)      o = Texture2DSampleGrad(Ov1, Ov1Sampler, UVB, gxB, gyB);
    else if (slot < 2.5) o = Texture2DSampleGrad(Ov2, Ov2Sampler, UVB, gxB, gyB);
    else if (slot < 3.5) o = Texture2DSampleGrad(Ov3, Ov3Sampler, UVB, gxB, gyB);
    else if (slot < 4.5) o = Texture2DSampleGrad(Ov4, Ov4Sampler, UVB, gxB, gyB);
    else if (slot < 5.5) o = Texture2DSampleGrad(Ov5, Ov5Sampler, UVB, gxB, gyB);
    else if (slot < 6.5) o = Texture2DSampleGrad(Ov6, Ov6Sampler, UVB, gxB, gyB);
    else if (slot < 7.5) o = Texture2DSampleGrad(Ov7, Ov7Sampler, UVB, gxB, gyB);
    else                 o = Texture2DSampleGrad(Ov8, Ov8Sampler, UVB, gxB, gyB);
    return float4(lerp(a.rgb, o.rgb, saturate(o.a * Factor.x)), a.a);
}
float4 b = Texture2DSampleGrad(BaseTex, BaseTexSampler, UVB, gxB, gyB);
float x = Factor.x;
float fr = frac(mode);
if (fr > 0.02 && fr < 0.98)
{
    float inner = fr * 5.0 - 1.25;
    x = (1.0 - abs(x)) / max(1.0 - inner, 1e-4);
}
// Painted alpha (Sprotile 5.14): the Tint vertex colour's alpha bends the edge. Alpha 1 everywhere (the default)
// leaves x alone; alpha painted toward 0 pushes B out by up to AlphaStrength patch widths. AlphaStrength 0 = off.
x += (1.0 - VertexAlpha) * AlphaStrength;
// A mesh without an Edge map gets Factor repeated into TexCoord 3 (Unreal copies the last channel into missing
// ones), so Edge == Factor means no edge: one instance can serve meshes with and without grass or snow.
if (UseEdges > 0.5 && any(Edge != Factor))
{
    float amount = 1.0 - Edge.y;
    float t = Edge.x;
    if (amount > 0.0)
    {
        float tri = 1.0 - abs(2.0 * frac(t) - 1.0);
        x -= tri * tri * tri * (0.8 + 0.2 * cos(t * 2.399963)) * amount;
    }
    else if (amount < 0.0)
    {
        x += (0.5 + 0.35 * cos(t * 6.2831853) + 0.15 * cos(t * 3.1415927)) * amount;
    }
}
float k = saturate(x);
if (mode > 1.5) k = k * k * (3.0 - 2.0 * k);
return lerp(a, b, k);