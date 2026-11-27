"""Path2D scripts run in both environments, devm's mock (tests/mock_test.py) and the deobf engine's
envlog (tests/deobf_corpus.py), with the same hand-computed answers: the two must agree.  Each script
prints `print(tag, value, ...)`; the expectations are per tag, numbers that are not exact (curve
lengths) last, compared within 0.005.
"""

# Frame 333 x 217.  frac: fractional offsets are truncated toward zero (10.7 -> 10, -3.2 -> -3,
# 30.9 -> 30, -20.5 -> -20, 5.5 -> 5, -7.8 -> -7): points (76.6, 83.8), (204.8, 101.5), the first
# one's right handle at (106.6, 63.8); the box of those widened by 0.5, and the length of that
# quadratic curve (132.949; with the offsets kept as given it would be 132.776).
# lerp: (0.1, 3, 0.2, -7):Lerp((0.3, 8, 0.4, 2), 0.5) = (0.2, 6, 0.3, -2) (5.5 and -2.5 rounded with
# floor(x + 0.5)): point (66.6 + 6, 65.1 - 2) = (72.6, 63.1) and (250, 180); length 212.453.
# (Rect text: the engine's float32 corners printed with %.9g.)
PATH2D_STORED = '''
local sg = Instance.new("ScreenGui")
local f = Instance.new("Frame")
f.Size = UDim2.fromOffset(333, 217)
f.Parent = sg
local p = Instance.new("Path2D")
p.Parent = f
p:SetControlPoints({ Path2DControlPoint.new(UDim2.new(0.2, 10.7, 0.4, -3.2), UDim2.new(), UDim2.fromOffset(30.9, -20.5)),
    Path2DControlPoint.new(UDim2.new(0.6, 5.5, 0.5, -7.8)) })
print("frac", tostring(p:GetBoundingRect()), p:GetLength())
local u = UDim2.new(0.1, 3, 0.2, -7):Lerp(UDim2.new(0.3, 8, 0.4, 2), 0.5)
p:SetControlPoints({ Path2DControlPoint.new(u), Path2DControlPoint.new(UDim2.fromOffset(250, 180)) })
print("lerp", tostring(u), tostring(p:GetBoundingRect()), p:GetLength())
'''
PATH2D_STORED_WANT = {
    'frac': ['76.0999985, 63.3000031, 205.300003, 102', 132.949],
    'lerp': ['{0.200000003, 6}, {0.300000012, -2}', '72.0999985, 62.6000061, 250.5, 180.5', 212.453],
}
# Points set before the path has a parent, then the Frame resized: (0.25, 0, 0.5, 0) and (0.75, 0,
# 0.5, 30) are (75, 100), (225, 130) in 300 x 200 (length sqrt(150^2 + 30^2) = 152.97...) and
# (150, 50), (450, 80) in 600 x 100 (length sqrt(300^2 + 30^2) = 301.496...; the middle of the
# line is at Scale 0.5).  One point: no segment, a stand-in Rect.
PATH2D_LATE = '''
local sg = Instance.new("ScreenGui")
local f = Instance.new("Frame")
f.Size = UDim2.fromOffset(300, 200)
f.Parent = sg
local p = Instance.new("Path2D")
p:SetControlPoints({ Path2DControlPoint.new(UDim2.fromScale(0.25, 0.5)), Path2DControlPoint.new(UDim2.new(0.75, 0, 0.5, 30)) })
p.Parent = f
print("late", tostring(p:GetBoundingRect()), string.format("%.3f", p:GetLength()))
f.Size = UDim2.fromOffset(600, 100)
print("resized", tostring(p:GetBoundingRect()), string.format("%.3f", p:GetLength()), p:GetPositionOnCurve(0.5).X.Scale)
local q = Instance.new("Path2D")
q.Parent = f
q:SetControlPoints({ Path2DControlPoint.new(UDim2.fromOffset(5, 5)) })
local s = q:GetBoundingRect()
print("one", typeof(s), type(s))
'''
PATH2D_LATE_WANT = {
    'late': ['74.5, 99.5, 225.5, 130.5', '152.971'],
    'resized': ['149.5, 49.5, 450.5, 80.5', '301.496', 0.5],
    'one': ['Rect', 'userdata'],
}
