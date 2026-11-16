-- message_1 -- recovered from its Luraph v15.1 VM (bootstrap replayed, payload lifted and decompiled).
--
-- Complete: all 131 functions of the protected program, no lift errors, no unstructured control flow.
-- The file parses as Luau; it was not run in the game.
--
-- What is and is not original:
--   * control flow, calls, strings, numbers and table contents are the script's own;
--   * local variable and parameter names do not survive protection.  Names such as `localPlayer` or
--     `humanoid` are derived from what the variable is initialised with, the rest are numbered (v12, p3);
--   * comments and formatting are gone; `local function f` appears as `local f = function`;
--   * Luraph's own additions (environment checks, self-modifying jumps, opaque-predicate guards and the
--     number variables they test) are removed.

local Players = game:GetService("Players")
local RunService = game:GetService("RunService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local UserInputService = game:GetService("UserInputService")
local TweenService = game:GetService("TweenService")
local CoreGui = game:GetService("CoreGui")
local localPlayer = Players.LocalPlayer
local v12 = {
	SpeedCap = 1.15,
	HopRatio = 1.515,
	HopMin = 40,
	HopGap = 0.06,
	HopGapMin = 0.06,
	HopGapMax = 0.2,
	HopRetries = 6,
	HopLift = 42,
	LandOffset = 14,
	LandSettle = 0.08,
	DropDelay = 0.05,
	GrabInterval = 0.03,
	RegrabFar = 40,
	Height = 70,
	ClimbShare = 0.5,
	CarryRatio = 0.9,
	EasyRatio = 1.3,
}
local v13 = { 60, 30, 0.15 }
local v15 = function(p1)
	local ok, result = pcall(function()
		return require(p1())
	end)
	return ok and result or nil
end
local networking = ReplicatedStorage:WaitForChild("Packages"):WaitForChild("Networking")
local v17 = v15(function()
	return ReplicatedStorage.Client.EggState
end)
local v19 = v15(function()
	return ReplicatedStorage.Data.Assets
end)
local v20 = v15(function()
	return ReplicatedStorage.Shared.Modules.Mutations
end)
local v23 = function(p2)
	return networking:FindFirstChild(p2)
end
local v24 = function()
	local character = localPlayer.Character
	return character and character:FindFirstChild("HumanoidRootPart")
end
local v26 = { Original = nil, Clone = nil, Links = {}, Connection = nil, Added = nil }
local v28 = function()
	local character2 = localPlayer.Character
	local humanoid = character2 and character2:FindFirstChildOfClass("Humanoid")
	local v184 = humanoid and humanoid.WalkSpeed or 16
	if v26.Original and 0 < v26.Original.Health then
		v184 = math.min(v184, v26.Original.WalkSpeed)
	end
	local ok2, result2 = pcall(function()
		local leaderstats = localPlayer:FindFirstChild("leaderstats")
		local speed = leaderstats and leaderstats:FindFirstChild("Speed")
		return speed and require(ReplicatedStorage.Shared.Util.TreadmillUtil).SpeedPowerToWalkSpeed(speed.Value) or nil
	end)
	if ok2 and type(result2) == "number" and 0 < result2 then
		v184 = math.min(v184, result2)
	end
	return v184
end
local v31 = function(p3)
	pcall(function()
		local playerScripts = localPlayer:FindFirstChild("PlayerScripts")
		local playerModule = playerScripts and playerScripts:FindFirstChild("PlayerModule")
		if playerModule then
			local v200 = require(playerModule):GetControls()
			if type(v200) == "table" then
				v200.humanoid = p3
			end
		end
	end)
end
local v32 = function(p4)
	local animate = p4 and p4:FindFirstChild("Animate")
	if animate and animate:IsA("LocalScript") then
		task.spawn(function()
			animate.Enabled = false
			task.wait()
			animate.Enabled = true
		end)
	end
end
local v33 = function()
	for key, value in ipairs(v26.Links) do
		pcall(function()
			value:Disconnect()
		end)
	end
	table.clear(v26.Links)
end
local v34 = {}
v34[Enum.HumanoidStateType.Running] = true
v34[Enum.HumanoidStateType.RunningNoPhysics] = true
v34[Enum.HumanoidStateType.Landed] = true
local v37 = function(p5)
	if not p5 or p5.Health <= 0 or p5.FloorMaterial == Enum.Material.Air then
		return false
	end
	return v34[p5:GetState()] == true
end
local v40 = function()
	local character3 = localPlayer.Character
	local humanoid2 = character3 and character3:FindFirstChildOfClass("Humanoid")
	if not humanoid2 or humanoid2.Health <= 0 then
		return
	end
	if v26.Clone and v26.Clone.Parent == character3 then
		return
	end
	if not v37(humanoid2) then
		return
	end
	local v225 = humanoid2:Clone()
	humanoid2.Parent = nil
	v225.Parent = character3
	workspace.CurrentCamera.CameraSubject = v225
	v31(v225)
	v32(character3)
	v26.Original = humanoid2
	v26.Clone = v225
	table.insert(v26.Links, humanoid2:GetPropertyChangedSignal("WalkSpeed"):Connect(function()
		if v225.Parent ~= nil then
			v225.WalkSpeed = humanoid2.WalkSpeed
		end
	end))
	local animator = humanoid2:FindFirstChildOfClass("Animator")
	local animator2 = v225:FindFirstChildOfClass("Animator")
	if animator and animator2 then
		table.insert(v26.Links, animator.AnimationPlayed:Connect(function(p6)
			local animation = p6.Animation
			if not animation or v225.Parent == nil then
				return
			end
			local ok3, result3 = pcall(function()
				return animator2:LoadAnimation(animation)
			end)
			if not ok3 or not result3 then
				return
			end
			pcall(function()
				result3.Priority = p6.Priority
				result3.Looped = p6.Looped
				result3:Play(0.05, math.max(p6.WeightTarget, 0.01), p6.Speed)
			end)
			local v243 = nil
			v243 = p6.Stopped:Connect(function()
				v243:Disconnect()
				pcall(function()
					result3:Stop(0.1)
				end)
			end)
		end))
	end
	table.insert(v26.Links, v225.Died:Connect(function()
		v33()
		v26.Original = nil
		v26.Clone = nil
		local character4 = localPlayer.Character
		if character4 and humanoid2.Parent == nil then
			humanoid2.Parent = character4
			workspace.CurrentCamera.CameraSubject = humanoid2
			v31(humanoid2)
		end
		pcall(function()
			v225:Destroy()
		end)
		humanoid2.Health = 0
	end))
end
local v41 = function()
	v40()
	local v259 = 0
	v26.Connection = RunService.Heartbeat:Connect(function(p7)
		v259 += p7
		local character5 = localPlayer.Character
		if (not (v26.Clone and character5 and v26.Clone.Parent == character5) and 0.25 or 3) <= v259 then
			v259 = 0
			v40()
		end
	end)
	v26.Added = localPlayer.CharacterAdded:Connect(function(p8)
		v33()
		v26.Original = nil
		v26.Clone = nil
		task.spawn(function()
			p8:WaitForChild("Humanoid", 10)
			task.wait(1)
			if v26.Connection and localPlayer.Character == p8 then
				v40()
			end
		end)
	end)
end
local v42 = {
	Carrying = false,
	Uid = nil,
	Delivered = 0,
	Busy = false,
	Cancel = false,
	Mult = 1,
	PulledAt = 0,
	HeldSeen = 0,
	GuessedDrop = false,
}
if type(v17) == "table" and type(v17.CarryChanged) == "table" and type(v17.CarryChanged.Connect) == "function" then
	v17.CarryChanged:Connect(function(p9)
		local v274 = type(p9) == "table" and p9.IsCarrying == true
		if v274 and p9.GuardDisabled == true then
			v274 = false
		end
		v42.GuessedDrop = false
		if v274 then
			v42.HeldSeen = os.clock()
		end
		if v274 and type(p9.Uid) == "string" then
			v42.Uid = p9.Uid
			local v275 = tonumber(p9.SpeedMultiplier)
			if v275 and 0 < v275 then
				v42.Mult = v275
			end
		end
		v42.Carrying = v274
	end)
end
pcall(function()
	v23("RE/EggWorld/FieldEggRedeemVerdict").OnClientEvent:Connect(function()
		v42.Delivered = os.clock()
	end)
end)
pcall(function()
	v23("RE/RigSync/Refresh").OnClientEvent:Connect(function(p10)
		if type(p10) == "table" and p10.Action == "Relocate" then
			v42.PulledAt = os.clock()
		end
	end)
end)
local v45 = function(p11)
	if type(p11) == "string" and type(v17) == "table" and type(v17.CarryFieldEgg) == "function" then
		pcall(v17.CarryFieldEgg, p11)
	end
end
local v46 = function()
	if type(v17) == "table" and type(v17.DropFieldEgg) == "function" then
		pcall(v17.DropFieldEgg, "PlayerRequest")
	end
end
local v49 = function(p12, p13)
	local v290 = nil
	local v291 = p13
	for key2, value2 in ipairs(workspace:GetChildren()) do
		if value2.Name == "SmartPromptPart" and value2:IsA("BasePart") then
			local carryAreaEgg = value2:FindFirstChild("CarryAreaEgg")
			if carryAreaEgg and carryAreaEgg:IsA("ProximityPrompt") then
				local v298 = (value2.Position - p12).Magnitude
				if v298 < v291 then
					v290 = carryAreaEgg
					v291 = v298
				end
			end
		end
	end
	return v290
end
local v50 = function(p14)
	local character6 = localPlayer.Character
	if type(p14) ~= "string" or not character6 then
		return false
	end
	local v304 = workspace:FindFirstChild(p14)
	if not v304 then
		return false
	end
	for key3, value3 in ipairs(v304:GetDescendants()) do
		if value3:IsA("WeldConstraint") or value3:IsA("JointInstance") then
			local v313, v314, v315 = pcall(function()
				return value3.Part0, value3.Part1
			end)
			if v313 and (v314 and v314:IsDescendantOf(character6) or v315 and v315:IsDescendantOf(character6)) then
				return true
			end
		end
	end
	return false
end
task.spawn(function()
	while true do
		task.wait(0.2)
		if v42.Carrying then
			if v50(v42.Uid) then
				v42.HeldSeen = os.clock()
			elseif 0.8 < os.clock() - v42.HeldSeen then
				v42.Carrying = false
				v42.GuessedDrop = true
			end
		elseif v42.GuessedDrop and v50(v42.Uid) then
			local v317 = os.clock()
			v42.GuessedDrop = false
			v42.Carrying = true
			v42.HeldSeen = v317
		end
	end
end)
local v52 = function()
	local v319 = {}
	local ok4, result4 = pcall(function()
		return v23("RF/EggWorld/AskFieldEggSnapshot"):InvokeServer()
	end)
	local v323 = ok4 and type(result4) == "table" and result4.Records or nil
	if type(v323) ~= "table" then
		return v319
	end
	for key4, value4 in pairs(v323) do
		if type(value4) == "table" and type(value4.Uid) == "string" and (value4.State == "Slot" or value4.State == "Dropped") and typeof(value4.BottomCFrame) == "CFrame" then
			v319[#v319 + 1] = value4
		end
	end
	return v319
end
local v53 = function(p15)
	for key5, value5 in ipairs(v52()) do
		if value5.Uid == p15 then
			return value5.BottomCFrame.Position
		end
	end
	return nil
end
local v54 = function(p16)
	local v335 = type(v19) == "table" and v19.Directory or nil
	local v336 = type(v335) == "table" and v335[tostring(p16.AssetCategory)] or nil
	local v337 = {
		Uid = p16.Uid,
		Category = tostring(p16.AssetCategory),
		Name = tostring(p16.AssetCategory),
		Icon = "",
		Rarity = 0,
		Value = 0,
	}
	if type(v336) == "table" then
		v337.Name = tostring(v336.DisplayName or p16.AssetCategory)
		local icon = v336.Icon
		if icon ~= nil and tostring(icon) ~= "" then
			local v344 = tostring(icon)
			local v345 = tonumber(v344) and "rbxassetid://" .. v344 or v344
			v337.Icon = v345
		end
		if type(v336.Rarity) == "table" then
			local v348 = tonumber(v336.Rarity.RarityNumber or v336.Rarity.Rank) or 0
			v337.Rarity = v348
		end
		local v349 = tonumber(p16.AssetScale) or 1
		local v350 = v349 > 5 and (v349 / 5) ^ 1.2 * 19.637875755794113 or v349 ^ 1.85
		local v351 = 1
		if type(v20) == "table" and type(v20.EarningsFor) == "function" then
			local ok5, result5 = pcall(v20.EarningsFor, type(p16.Mutations) == "table" and p16.Mutations or {})
			if ok5 and type(result5) == "number" then
				v351 = result5
			end
		end
		local v357 = tonumber(v336.EarningRate) or 0
		v337.Value = v357 * v350 * v351
	end
	return v337
end
local v55 = function(p17)
	for key6, value6 in ipairs({ { 1000000000000, "T" }, { 1000000000, "B" }, { 1000000, "M" }, { 1000, "K" } }) do
		if value6[1] <= p17 then
			return string.format("$%.2f%s/s", p17 / value6[1], value6[2])
		end
	end
	return string.format("$%d/s", math.floor(p17 + 0.5))
end
local v57 = function(p18)
end
local v58 = function()
	local currentCamera = workspace.CurrentCamera
	if not currentCamera then
		return function()
		end
	end
	local cameraType = currentCamera.CameraType
	local cFrame = currentCamera.CFrame
	pcall(function()
		currentCamera.CameraType = Enum.CameraType.Scriptable
		currentCamera.CFrame = cFrame
	end)
	return function()
		pcall(function()
			currentCamera.CameraType = cameraType
		end)
	end
end
local v59 = 0
local v62 = function()
	if typeof(setfpscap) ~= "function" then
		return
	end
	v59 += 1
	local v383 = v59
	task.spawn(function()
		local v386 = true
		local v387 = os.clock()
		while v59 == v383 and os.clock() - v387 < 60 do
			pcall(setfpscap, v386 and v13[1] or v13[2])
			v386 = not v386
			task.wait(v13[3])
		end
	end)
end
local v65 = function()
	v59 += 1
	if typeof(setfpscap) == "function" then
		pcall(setfpscap, 240)
	end
end
local v66 = nil
local v67 = function()
	local v395 = v66
	v66 = nil
	if v395 then
		pcall(function()
			v395:Destroy()
		end)
	end
end
local v68 = function()
	v67()
	local character7 = localPlayer.Character
	if not character7 then
		return
	end
	local archivable = character7.Archivable
	character7.Archivable = true
	local v405 = character7:Clone()
	character7.Archivable = archivable
	if v405 then
		for key7, value7 in ipairs(v405:GetDescendants()) do
			if value7:IsA("LuaSourceContainer") or value7:IsA("Humanoid") then
				pcall(function()
					value7:Destroy()
				end)
			elseif value7:IsA("BasePart") then
				value7.Anchored = true
				value7.CanCollide = false
				value7.CanTouch = false
				value7.CanQuery = false
			end
		end
		v405.Name = "Clone"
		v405.Parent = workspace
		v66 = v405
	end
end
local v71 = function()
	local objects = workspace:FindFirstChild("World") or workspace:FindFirstChild("__OBJECTS")
	local areas = objects and objects:FindFirstChild("Areas")
	local separationLine = areas and areas:FindFirstChild("SeparationLine")
	local v419 = separationLine and separationLine:IsA("BasePart")
	return v419 and separationLine.Position.X or 552.2, v419 and separationLine.Position.Y or 67.67
end
local v74 = function()
	for key8, value8 in ipairs({
		{ { "GearGiver_Slap", "Podium" }, Vector3.new(-16.415, 21.072, -6.106) },
		{
		{ "World", "Machines", "RiftMachine", "Rift", "Meshes/VoidPortal_Cube.003" },
		Vector3.new(-26.776, 1.75, 18.665),
	},
		{
		{ "__OBJECTS", "Machines", "RiftMachine", "Rift", "Meshes/VoidPortal_Cube.003" },
		Vector3.new(-26.776, 1.75, 18.665),
	},
	}) do
		local v430 = workspace
		for key9, value9 in ipairs(value8[1]) do
			v430 = v430 and v430:FindFirstChild(value9) or nil
		end
		if v430 and v430:IsA("BasePart") then
			return v430.CFrame:PointToWorldSpace(value8[2])
		end
	end
	return Vector3.new(528.7, 70.57, -364.11)
end
local v75 = function(p19)
	local v438 = v24()
	if not v438 then
		return
	end
	pcall(function()
		v438.CFrame = CFrame.new(p19) * CFrame.Angles(0, math.rad(90), 0)
		v438.AssemblyLinearVelocity = Vector3.zero
		v438.AssemblyAngularVelocity = Vector3.zero
	end)
end
local v76 = {}
local v77 = 0
local v80 = function()
	if os.clock() - v77 < 1 then
		return v76
	end
	v77 = os.clock()
	local v446 = {}
	local v447 = function(p20)
		local v480, v481, v482 = pcall(function()
			if p20:IsA("Model") then
				return p20:GetBoundingBox()
			end
			if p20:IsA("BasePart") then
				return p20.CFrame, p20.Size
			end
		end)
		if v480 and v481 and v482 then
			local v483 = Vector3.new(math.abs(v482.X), 0, math.abs(v482.Z)) * 0.5
			local v484 = (v481 - v481.Position):VectorToWorldSpace(v483)
			local v485 = math.max(math.abs(v484.X), v483.X, v483.Z)
			local v486 = math.max(math.abs(v484.Z), v483.X, v483.Z)
			v446[#v446 + 1] = {
				MinX = v481.Position.X - v485,
				MaxX = v481.Position.X + v485,
				MinZ = v481.Position.Z - v486,
				MaxZ = v481.Position.Z + v486,
			}
		end
	end
	local v450 = function(p21)
		if p21 == "ScrambleLocalVisuals" or p21 == "DrScrambleEvent" then
			return false
		end
		local v491 = string.lower(p21)
		return string.find(v491, "portal", 1, true) or string.find(v491, "teleport", 1, true) or string.find(v491, "mech", 1, true) or string.find(v491, "arena", 1, true) or string.find(v491, "scramble", 1, true)
	end
	for key10, value10 in ipairs(workspace:GetChildren()) do
		if (value10:IsA("Model") or value10:IsA("BasePart") or value10:IsA("Folder")) and v450(value10.Name) then
			if value10:IsA("Folder") then
				for key11, value11 in ipairs(value10:GetChildren()) do
					v447(value11)
				end
			else
				v447(value10)
			end
		end
	end
	local world = workspace:FindFirstChild("World")
	local build = world and world:FindFirstChild("Build")
	if build then
		for key12, value12 in ipairs(build:GetChildren()) do
			if v450(value12.Name) then
				for key13, value13 in ipairs(value12:GetChildren()) do
					v447(value13)
				end
			end
		end
	end
	v76 = v446
	return v446
end
local v81 = function(p22, p23)
	for key14, value14 in ipairs(v80()) do
		local v499 = value14.MinX - 12
		local v500 = value14.MaxX + 12
		local v501 = value14.MinZ - 12
		local v502 = value14.MaxZ + 12
		if not (p22.X >= v499 and p22.X <= v500 and p22.Z >= v501 and p22.Z <= v502) then
			local v504 = 0
			local v505 = 1
			local v506 = true
			for key15, value15 in ipairs({ { p22.X, p23.X - p22.X, v499, v500 }, { p22.Z, p23.Z - p22.Z, v501, v502 } }) do
				local v518 = value15[1]
				local v519 = value15[2]
				local v520 = value15[3]
				local v521 = value15[4]
				if math.abs(v519) < 1e-06 then
					if v518 < v520 or v521 < v518 then
						v506 = false
					end
				else
					local v522 = (v520 - v518) / v519
					local v523 = (v521 - v518) / v519
					if v523 < v522 then
						local v524 = v522
						v522 = v523
						v523 = v524
					end
					v504 = math.max(v504, v522)
					v505 = math.min(v505, v523)
					if v505 < v504 then
						v506 = false
					end
				end
			end
			if v506 then
				local v512 = v501 - 2
				local v513 = v502 + 2
				local v514 = math.abs(p22.Z - v512) <= math.abs(p22.Z - v513) and v512 or v513
				if v514 < -440 or -290 < v514 then
					v514 = v514 == v512 and v513 or v512
				end
				local v516 = math.abs(p22.X - v499) <= math.abs(p22.X - v500) and v499 or v500
				if math.abs(p22.Z - v514) < 3 then
					v516 = math.abs(p23.X - v499) <= math.abs(p23.X - v500) and v499 or v500
				end
				return Vector3.new(v516, p23.Y, v514)
			end
		end
	end
	return p23
end
local v83 = function(p24, p25)
	local v526 = v71()
	local v527 = v74()
	local character8 = localPlayer.Character
	local humanoid3 = character8 and character8:FindFirstChildOfClass("Humanoid")
	if humanoid3 then
		humanoid3.PlatformStand = false
		if character8:FindFirstChildWhichIsA("Tool") then
			pcall(function()
				humanoid3:UnequipTools()
			end)
		end
	end
	local v530 = v24()
	if not v530 then
		return false
	end
	local v531 = "field"
	if v530.Position.X < v526 - 2 and 20 < Vector3.new(v530.Position.X - v527.X, 0, v530.Position.Z - v527.Z).Magnitude then
		v531 = "safe"
	end
	local v532 = os.clock()
	local v533 = os.clock()
	local position = v530.Position
	local v535 = 0
	while true do
		if os.clock() - v532 < 120 and not v42.Cancel then
			v530 = v24()
			if not v530 then
				break
			end
			local v539 = Vector3.new(p25.X - v530.Position.X, 0, p25.Z - v530.Position.Z)
			if v531 ~= "field" or not (v539.Magnitude <= 2.5) then
				local v540 = p25
				if v531 == "safe" then
					if not (Vector3.new(v527.X - v530.Position.X, 0, v527.Z - v530.Position.Z).Magnitude <= 6) then
						v540 = v527
					else
						v531 = "field"
					end
				end
				local v542 = v81(v530.Position, v540)
				local v543 = Vector3.new(v542.X - v530.Position.X, 0, v542.Z - v530.Position.Z)
				local v544 = v543.Magnitude > 0.01 and v543.Unit or Vector3.zero
				local v545 = v544 * math.min(math.max(v28() * v12.SpeedCap, 8), v543.Magnitude / 0.05)
				pcall(function()
					v530.AssemblyLinearVelocity = Vector3.new(v545.X, v530.AssemblyLinearVelocity.Y, v545.Z)
					if humanoid3 and 0 < v544.Magnitude then
						humanoid3:Move(v544, false)
					end
				end)
				if 1.5 <= os.clock() - v533 then
					if (v530.Position - position).Magnitude < 3 and humanoid3 and 15 < v539.Magnitude then
						pcall(function()
							humanoid3.Jump = true
						end)
					end
					position = v530.Position
					v533 = os.clock()
				end
				if v531 == "field" and v539.Magnitude <= 9 and 0.1 < os.clock() - v535 then
					v535 = os.clock()
					v45(p24)
				end
				RunService.Heartbeat:Wait()
				continue
			end
		end
		v530 = v24()
		if v530 then
			pcall(function()
				v530.AssemblyLinearVelocity = Vector3.new(0, v530.AssemblyLinearVelocity.Y, 0)
				if humanoid3 then
					humanoid3:Move(Vector3.zero, false)
				end
			end)
		end
		return v42.Carrying or v530 ~= nil and Vector3.new(p25.X - v530.Position.X, 0, p25.Z - v530.Position.Z).Magnitude <= 6
	end
	return false
end
local v86 = function(p26, p27)
	local v553 = workspace:FindFirstChild(p26)
	local areaEggSlotsClient = workspace:FindFirstChild("AreaEggSlotsClient")
	local v557 = v553 or areaEggSlotsClient and areaEggSlotsClient:FindFirstChild(p26)
	if v557 then
		local ok6, result6 = pcall(function()
			return v557:GetPivot().Position
		end)
		if ok6 and result6 then
			local v563 = os.clock()
			p27.Pos = result6
			p27.At = v563
			return result6
		end
	end
	if 0.5 <= os.clock() - p27.At then
		p27.At = os.clock()
		local v564 = v53(p26) or p27.Pos
		p27.Pos = v564
	end
	return p27.Pos
end
local v87 = function(p28, p29)
	local v566 = { At = 0 }
	local v569 = 0
	local v570 = 1
	while not v42.Carrying and v569 < p29 and not v42.Cancel do
		local v572 = v24()
		local v573 = v86(p28, v566)
		local v574 = math.max(RunService.Heartbeat:Wait(), 0.004166666666666667)
		v569 += v574
		v570 += v574
		if not v572 or not v573 then
			if v12.GrabInterval <= v570 then
				v570 = 0
				task.spawn(v45, p28)
			end
		else
			local v575 = v573 - v572.Position
			if 2 < v575.Magnitude then
				local v576 = math.max(v28() * v12.SpeedCap, 16)
				local v577 = v575 / math.max(0.08, v574)
				if v576 < v577.Magnitude then
					v577 = v577.Unit * v576
				end
				pcall(function()
					v572.AssemblyLinearVelocity = v577 + Vector3.new(0, workspace.Gravity * v574 * 0.5, 0)
					v572.AssemblyAngularVelocity = Vector3.zero
				end)
			end
			if v12.GrabInterval <= v570 then
				v570 = 0
				local v579 = v49(v573 - Vector3.new(0, 3, 0), 10)
				if v579 and typeof(fireproximityprompt) == "function" then
					pcall(function()
						v579.HoldDuration = 0
					end)
					pcall(fireproximityprompt, v579)
				end
				task.spawn(v45, p28)
			end
		end
	end
	local v580 = v24()
	if v580 then
		pcall(function()
			v580.AssemblyLinearVelocity = Vector3.zero
		end)
	end
	return v42.Carrying and v42.Uid == p28
end
local v88 = function(p30)
	if v42.Carrying then
		return true
	end
	local v585 = v24()
	local v586 = v53(p30)
	if not v585 or not v586 or not (v12.RegrabFar < Vector3.new(v585.Position.X - v586.X, 0, v585.Position.Z - v586.Z).Magnitude) then
		v57("The egg fell close, taking it back")
	else
		v57("The egg fell far behind, teleporting onto it")
		v75(v586 + Vector3.new(0, 3, 0))
	end
	return v87(p30, 3)
end
local v91 = function(p31, p32)
	local v588 = p32
	pcall(function()
		local v591 = RaycastParams.new()
		v591.FilterType = Enum.RaycastFilterType.Exclude
		v591.FilterDescendantsInstances = { localPlayer.Character }
		v591.IgnoreWater = true
		local v592 = workspace:Raycast(p31 + Vector3.new(0, 60, 0), Vector3.new(0, -140, 0), v591)
		if v592 and v592.Material ~= Enum.Material.Water and math.abs(v592.Position.Y - p32) < 40 then
			v588 = v592.Position.Y + 3.5
		end
	end)
	return v588
end
local v93 = function(p33)
	local v594 = v28()
	local v595 = v594 * math.min(v12.CarryRatio, v12.SpeedCap) * v42.Mult
	local v596 = v595 * 1.5
	local v597 = 5.5 * v595
	local v598 = v596
	if p33 and v597 < p33 then
		v598 = math.min(v596, v595 * p33 / (p33 - v597))
	end
	return math.min(math.max(math.min(v595 * v12.EasyRatio, v598), v595), math.max(v594 * v12.SpeedCap, v595))
end
local v96 = function(p34, p35)
	local v600 = os.clock()
	local v601 = v74()
	local v602 = p34 - 7
	local height = v12.Height
	local v604 = math.clamp(v12.ClimbShare, 0.1, 0.9)
	local v605 = height * math.sqrt(1 - v604 * v604) / v604
	local character9 = localPlayer.Character
	local humanoid4 = character9 and character9:FindFirstChildOfClass("Humanoid")
	if humanoid4 then
		humanoid4.PlatformStand = false
	end
	local v609 = v24()
	if v609 and character9 and 0.5 < height and v609.Position.Y < v601.Y + height - 2 then
		pcall(function()
			character9:PivotTo(CFrame.new(Vector3.new(v609.Position.X, v601.Y + height, v609.Position.Z)) * v609.CFrame.Rotation)
			v609.AssemblyLinearVelocity = Vector3.zero
			v609.AssemblyAngularVelocity = Vector3.zero
		end)
	end
	local v610 = v24()
	local v613 = v93(v610 and Vector3.new(v610.Position.X - v601.X, 0, v610.Position.Z - v601.Z).Magnitude + math.max(0, height) * 2 or nil)
	local v614 = os.clock()
	local v615 = 0
	v57("Safe zone")
	while true do
		if v42.Carrying and v42.Delivered < v600 and not v42.Cancel and v615 < 25 then
			v609 = v24()
			if not v609 then
				break
			end
			local v619 = os.clock()
			local v620 = math.max(v619 - v614, 0.004166666666666667)
			v614 = v619
			v615 += v620
			local v621 = v609.Position.X <= v602 + 2
			local v622 = v621 and v601 or Vector3.new(v602, v601.Y, p35)
			if not v621 or not (Vector3.new(v601.X - v609.Position.X, 0, v601.Z - v609.Position.Z).Magnitude < 2) then
				local v623 = v81(v609.Position, v622)
				local v624 = Vector3.new(v623.X - v609.Position.X, 0, v623.Z - v609.Position.Z)
				local v625 = v621 and 0 or math.max(0, v609.Position.X - v602)
				local v626 = v601.Y + height
				if v621 or v625 <= v605 then
					v626 = v601.Y + height * math.clamp(v625 / math.max(v605, 1), 0, 1)
				end
				local v627 = math.clamp((v626 - v609.Position.Y) / 0.12, -v613 * v604, v613 * v604)
				local v628 = math.sqrt(math.max(v613 * v613 - v627 * v627, 0))
				local v629 = v624.Magnitude > 0.01 and v624.Unit * math.min(v628, v624.Magnitude / 0.05) or Vector3.zero
				pcall(function()
					v609.AssemblyLinearVelocity = Vector3.new(v629.X, v627, v629.Z)
				end)
				RunService.Heartbeat:Wait()
				continue
			end
		end
		if humanoid4 then
			pcall(function()
				humanoid4:Move(Vector3.zero, false)
			end)
		end
		local v630 = 0
		while v630 < 2 and v42.Delivered < v600 and v42.Carrying and not v42.Cancel do
			v630 += RunService.Heartbeat:Wait()
		end
		if v42.Carrying and v42.Delivered < v600 and not v42.Cancel then
			task.wait(0.2)
			v46()
			local v631 = 0
			while v42.Delivered < v600 and v631 < 1 do
				v631 += RunService.Heartbeat:Wait()
			end
		end
		return v42.Delivered >= v600
	end
	return false
end
local v99 = function(p36)
	local v635 = os.clock()
	local v636, v637 = v71()
	local v638 = v24()
	if not v638 then
		return false
	end
	local v639 = math.clamp(v638.Position.Z, -425, -300)
	local v640 = Vector3.new(v636 + v12.LandOffset, v637 + 3.35, v639)
	local v641 = math.max(v28() * v12.HopRatio, v12.HopMin)
	local v642 = v638.Position.Y + v12.HopLift
	local x = v638.Position.X
	local v644 = 0
	local v645 = v58()
	v42.ReleaseCamera = v645
	pcall(v68)
	v62()
	while v640.X < x - v641 and not v42.Cancel do
		if v42.Carrying then
			local v647 = x - v641
			v57(string.format("Instant TP: hopping home, X %d", math.floor(v647)))
			local pulledAt = v42.PulledAt
			local v649 = 0
			while v649 < v12.HopGap do
				v75(Vector3.new(v647, v642, v639))
				v649 += RunService.Heartbeat:Wait()
			end
			local v650 = v24()
			if not (v42.PulledAt > pulledAt or v650 ~= nil and (v650.Position.X - v647 > 10 or v650.AssemblyLinearVelocity.Magnitude > 150)) or not v42.Carrying or v42.Cancel then
				x = v647
				v12.HopGap = math.max(v12.HopGapMin, v12.HopGap - 0.01)
			else
				v644 += 1
				v12.HopGap = math.min(v12.HopGapMax, v12.HopGap + 0.02)
				if v12.HopRetries < v644 then
					break
				end
				v57(string.format("Instant TP: pulled back, retry %d/%d", v644, v12.HopRetries))
				local v654 = 0
				while v654 < 0.12 and not v42.Cancel do
					v75(Vector3.new(x, v642, v639))
					v654 += RunService.Heartbeat:Wait()
				end
			end
		else
			if not v88(p36) then
				break
			end
			local v646 = v24()
			if v646 then
				x = math.min(x, v646.Position.X)
			end
		end
	end
	v57("Instant TP: landing next to the line")
	local v655 = Vector3.new(v640.X, v91(v640, v640.Y), v640.Z)
	local v658 = function()
		local v668 = v24()
		if not v668 or not (v668.Position.X <= v655.X + 1) or not (v655.Y - 25 < v668.Position.Y) then
			v75(v655)
			return
		end
		pcall(function()
			v668.AssemblyLinearVelocity = Vector3.zero
		end)
	end
	v658()
	for i = 1, 3 do
		local v660 = 0
		while v660 < v12.LandSettle and not v42.Cancel do
			v660 += RunService.Heartbeat:Wait()
		end
		local v661 = v24()
		if not v42.Carrying or not v661 or not (12 < v661.Position.X - v655.X) and not (v661.Position.Y < v655.Y - 25) then
			break
		end
		v57("Instant TP: landing retry " .. i)
		v658()
	end
	v67()
	if v42.Carrying and not v42.Cancel then
		local v663 = 0
		while v663 < v12.DropDelay do
			v663 += RunService.Heartbeat:Wait()
		end
		v57("Instant TP: dropping the egg")
		v46()
		local v664 = 0
		while v42.Carrying and v664 < 1 and not v42.Cancel do
			v664 += RunService.Heartbeat:Wait()
		end
		local v665 = v24()
		if v665 then
			pcall(function()
				v665.AssemblyLinearVelocity = Vector3.zero
				v665.AssemblyAngularVelocity = Vector3.zero
			end)
		end
		v645()
		v67()
		v57("Instant TP: taking the egg back")
		if not v87(p36, 3) and not v88(p36) then
			v65()
			v57("Could not take the egg back")
			return false
		end
		v67()
	end
	v645()
	local v666 = false
	if v42.Carrying then
		v666 = v96(v636, v639)
	end
	v65()
	return v666 or v42.Delivered >= v635
end
local v100 = function(p37)
	if v42.Busy then
		return
	end
	v42.Busy = true
	v42.Cancel = false
	task.spawn(function()
		local ok7, result7 = pcall(function()
			v57("Looking for the egg")
			local v688 = v53(p37)
			if not v688 then
				v57("That egg is gone")
				return
			end
			if v42.Carrying and v42.Uid ~= p37 then
				v46()
				local v689 = 0
				while v42.Carrying and v689 < 1 do
					v689 += RunService.Heartbeat:Wait()
				end
			end
			v62()
			v57("Going to the egg")
			if not v83(p37, v688) then
				v65()
				v57("Could not reach the egg")
				return
			end
			v57("Taking the egg")
			if not v87(p37, 4) then
				v65()
				v57("The egg would not come free")
				return
			end
			v57(v99(p37) and "Delivered" or "Delivery failed")
		end)
		v65()
		v67()
		if v42.ReleaseCamera then
			pcall(v42.ReleaseCamera)
			v42.ReleaseCamera = nil
		end
		if not ok7 then
			v57("Error: " .. tostring(result7))
		end
		v42.Busy = false
	end)
end
local v101 = {
	BG = Color3.fromRGB(10, 18, 35),
	BG_TRANS = 0.25,
	SURF = Color3.fromRGB(18, 32, 60),
	SURF_TRANS = 0.2,
	SURF2 = Color3.fromRGB(28, 48, 85),
	SURF2_TRANS = 0.15,
	TEXT = Color3.fromRGB(220, 235, 255),
	AQUA_STROKE = Color3.fromRGB(80, 150, 255),
	GREEN1 = Color3.fromRGB(18, 88, 58),
	GREEN2 = Color3.fromRGB(21, 120, 76),
	GREEN_STROKE = Color3.fromRGB(60, 185, 120),
	OFF_BG = Color3.fromRGB(30, 45, 80),
	OFF_TEXT = Color3.fromRGB(120, 160, 220),
}
local v102 = {
	Color3.fromRGB(190, 190, 190),
	Color3.fromRGB(110, 210, 110),
	Color3.fromRGB(90, 160, 255),
	Color3.fromRGB(190, 110, 255),
	Color3.fromRGB(255, 170, 50),
	Color3.fromRGB(255, 80, 80),
	Color3.fromRGB(255, 90, 200),
	Color3.fromRGB(80, 235, 235),
}
local v104 = function()
	if typeof(gethui) == "function" then
		local ok8, result8 = pcall(gethui)
		if ok8 and typeof(result8) == "Instance" then
			return result8
		end
	end
	return CoreGui
end
local v107 = function(p38, p39)
	local v698 = Instance.new(p38)
	for key16, value16 in pairs(p39 or {}) do
		v698[key16] = value16
	end
	return v698
end
local v108 = function(p40, p41)
	local v707 = v107
	local v708 = "UICorner"
	local v709 = {}
	v709.CornerRadius = UDim.new(0, p41 or 6)
	local v713 = v707(v708, v709)
	v713.Parent = p40
	return v713
end
local v110 = function(p42, p43, p44, p45)
	local v715 = v107
	local v716 = "UIStroke"
	local v717 = { Color = p43 }
	v717.Thickness = p44 or 1
	v717.Transparency = p45 or 0
	v717.ApplyStrokeMode = Enum.ApplyStrokeMode.Border
	local v720 = v715(v716, v717)
	v720.Parent = p42
	return v720
end
local v112 = function(p46, p47, p48, p49, p50)
	local v724 = TweenService
	v724:Create(p46, TweenInfo.new(p47 or 0.2, p49 or Enum.EasingStyle.Quint, p50 or Enum.EasingDirection.Out), p48):Play()
end
local tokinuEgg = v104():FindFirstChild("TokinuEgg")
if tokinuEgg then
	tokinuEgg:Destroy()
end
local v114 = v107("ScreenGui", {
	Name = "TokinuEgg",
	ResetOnSpawn = false,
	ZIndexBehavior = Enum.ZIndexBehavior.Sibling,
	IgnoreGuiInset = true,
	DisplayOrder = 999,
})
v114.Parent = v104()
local v115 = v107("Frame", {
	Name = "TokinuEggFrame",
	Size = UDim2.fromOffset(240, 320),
	Position = UDim2.new(0.5, -120, 0.5, -160),
	BackgroundColor3 = v101.BG,
	BackgroundTransparency = v101.BG_TRANS,
	BorderSizePixel = 0,
	ClipsDescendants = false,
	Active = true,
})
v115.Parent = v114
v108(v115, 18)
v110(v115, v101.AQUA_STROKE, 1.2, 0.3)
v107("ImageLabel", {
	Name = "Shadow",
	AnchorPoint = Vector2.new(0.5, 0.5),
	Position = UDim2.new(0.5, 0, 0.5, 2),
	Size = UDim2.new(1, 24, 1, 24),
	BackgroundTransparency = 1,
	Image = "rbxassetid://6014261993",
	ImageColor3 = Color3.new(0, 0, 0),
	ImageTransparency = 0.72,
	ScaleType = Enum.ScaleType.Slice,
	SliceCenter = Rect.new(49, 49, 450, 450),
	ZIndex = -1,
	Active = false,
}).Parent = v115
local v116 = v107("Frame", {
	Name = "Header",
	Size = UDim2.new(1, 0, 0, 44),
	BackgroundTransparency = 1,
	ZIndex = 101,
	Active = true,
})
v116.Parent = v115
local v117 = v107("TextLabel", {
	Name = "Title",
	Size = UDim2.new(1, -28, 0, 24),
	Position = UDim2.new(0, 14, 0, 10),
	ZIndex = 102,
	BackgroundTransparency = 1,
	Text = "TOKINU | SAE",
	Font = Enum.Font.GothamBlack,
	TextSize = 16,
	TextColor3 = v101.TEXT,
	TextXAlignment = Enum.TextXAlignment.Center,
})
v117.Parent = v116
v107("UIGradient", { Color = ColorSequence.new(Color3.new(1, 1, 1), Color3.fromRGB(150, 200, 255)) }).Parent = v117
local v118 = v107("Frame", {
	Name = "Divider",
	AnchorPoint = Vector2.new(0.5, 0),
	Position = UDim2.new(0.5, 0, 0, 38),
	Size = UDim2.new(0, 124, 0, 1),
	BackgroundColor3 = Color3.fromRGB(150, 200, 255),
	BackgroundTransparency = 0.2,
	BorderSizePixel = 0,
	ZIndex = 101,
})
v118.Parent = v115
v107("UIGradient", { Color = ColorSequence.new(Color3.new(1, 1, 1), Color3.fromRGB(165, 175, 195)), Rotation = 90 }).Parent = v115
v107("UIGradient", {
	Transparency = NumberSequence.new({
	NumberSequenceKeypoint.new(0, 1),
	NumberSequenceKeypoint.new(0.2, 0.1),
	NumberSequenceKeypoint.new(0.8, 0.1),
	NumberSequenceKeypoint.new(1, 1),
}),
}).Parent = v118
;(function(p51, p52)
	local v733 = nil
	local v734 = nil
	local v735 = nil
	local v736 = nil
	local v737 = function(p53)
		local v739 = p53 - v735
		p51.Position = UDim2.new(v736.X.Scale, v736.X.Offset + v739.X, v736.Y.Scale, v736.Y.Offset + v739.Y)
	end
	p52.InputBegan:Connect(function(p54)
		if p54.UserInputType == Enum.UserInputType.MouseButton1 or p54.UserInputType == Enum.UserInputType.Touch then
			v733 = true
			v734 = p54
			v735 = p54.Position
			v736 = p51.Position
			p54.Changed:Connect(function()
				if p54.UserInputState == Enum.UserInputState.End then
					v733 = false
				end
			end)
		end
	end)
	UserInputService.InputChanged:Connect(function(p55)
		if v733 and (p55.UserInputType == Enum.UserInputType.MouseMovement or p55.UserInputType == Enum.UserInputType.Touch) then
			v737(p55.Position)
		end
	end)
end)(v115, v116)
local v121 = v107("TextButton", {
	Name = "FoldButton",
	AutoButtonColor = false,
	Size = UDim2.fromOffset(22, 22),
	Position = UDim2.new(1, -32, 0, 11),
	BackgroundColor3 = v101.OFF_BG,
	BackgroundTransparency = 0.1,
	BorderSizePixel = 0,
	Text = "-",
	Font = Enum.Font.GothamBold,
	TextSize = 16,
	TextColor3 = v101.TEXT,
	ZIndex = 105,
})
v121.Parent = v116
v108(v121, 7)
local v122 = v110(v121, v101.AQUA_STROKE, 1, 0.5)
v121.MouseEnter:Connect(function()
	v112(v121, 0.14, { BackgroundColor3 = Color3.fromRGB(40, 65, 115), BackgroundTransparency = 0.05 })
	v112(v122, 0.14, { Transparency = 0.3 })
end)
v121.MouseLeave:Connect(function()
	v112(v121, 0.14, { BackgroundColor3 = v101.OFF_BG, BackgroundTransparency = 0.1 })
	v112(v122, 0.14, { Transparency = 0.5 })
end)
local v124 = v107("Frame", {
	Name = "ContentBox",
	Size = UDim2.new(1, -20, 1, -52),
	Position = UDim2.fromOffset(10, 48),
	BackgroundColor3 = v101.SURF,
	BackgroundTransparency = v101.SURF_TRANS,
	BorderSizePixel = 0,
	ZIndex = 101,
})
v124.Parent = v115
v108(v124, 16)
v110(v124, v101.AQUA_STROKE, 1, 0.4)
local v125 = v107("Frame", {
	Name = "Inner",
	Size = UDim2.new(1, -10, 1, -10),
	Position = UDim2.fromOffset(5, 5),
	BackgroundTransparency = 1,
	ZIndex = 102,
})
v125.Parent = v124
local v126 = false
local v127 = nil
v121.MouseButton1Click:Connect(function()
	v126 = not v126
	if v126 then
		v124.Visible = false
		v127 = v115.Size
		v115.Size = UDim2.fromOffset(240, 44)
		v121.Text = "+"
	else
		v124.Visible = true
		if v127 then
			v115.Size = v127
			v127 = nil
		end
		v121.Text = "-"
	end
end)
local v128 = v107("ScrollingFrame", {
	Name = "EggList",
	Size = UDim2.new(1, 0, 1, -58),
	Position = UDim2.fromOffset(0, 0),
	BackgroundTransparency = 1,
	BorderSizePixel = 0,
	ScrollBarThickness = 3,
	ScrollBarImageColor3 = v101.AQUA_STROKE,
	CanvasSize = UDim2.new(),
	AutomaticCanvasSize = Enum.AutomaticSize.Y,
	ZIndex = 102,
})
v128.Parent = v125
v107("UIListLayout", { Padding = UDim.new(0, 6), SortOrder = Enum.SortOrder.LayoutOrder }).Parent = v128
v107("UIPadding", {
	PaddingTop = UDim.new(0, 5),
	PaddingBottom = UDim.new(0, 5),
	PaddingLeft = UDim.new(0, 5),
	PaddingRight = UDim.new(0, 6),
}).Parent = v128
local v129 = v107("TextLabel", {
	Name = "Status",
	Size = UDim2.new(1, 0, 0, 14),
	Position = UDim2.new(0, 0, 1, -54),
	BackgroundTransparency = 1,
	Font = Enum.Font.Gotham,
	TextSize = 9,
	TextColor3 = v101.OFF_TEXT,
	TextXAlignment = Enum.TextXAlignment.Left,
	TextTruncate = Enum.TextTruncate.AtEnd,
	Text = "Pick a pet",
	ZIndex = 103,
})
v129.Parent = v125
local v130 = v107("TextButton", {
	Name = "StealButton",
	Size = UDim2.new(1, 0, 0, 34),
	Position = UDim2.new(0, 0, 1, -34),
	BackgroundColor3 = v101.OFF_BG,
	BackgroundTransparency = 0.1,
	BorderSizePixel = 0,
	Font = Enum.Font.GothamBlack,
	TextSize = 12,
	TextColor3 = v101.TEXT,
	Text = "STEAL",
	AutoButtonColor = false,
	ZIndex = 104,
})
v130.Parent = v125
v108(v130, 11)
local v131 = v110(v130, v101.AQUA_STROKE, 1, 0.5)
v130.MouseEnter:Connect(function()
	v112(v130, 0.14, { BackgroundColor3 = Color3.fromRGB(40, 65, 115), BackgroundTransparency = 0.05 })
	v112(v131, 0.14, { Transparency = 0.3 })
end)
v130.MouseLeave:Connect(function()
	v112(v130, 0.14, { BackgroundColor3 = v101.OFF_BG, BackgroundTransparency = 0.1 })
	v112(v131, 0.14, { Transparency = 0.5 })
end)
local v134 = {
	["Pick a pet"] = true,
	["Looking for the egg"] = true,
	["Going to the egg"] = true,
	["Taking the egg"] = true,
	Delivered = true,
	["Delivery failed"] = true,
	["That egg is gone"] = true,
	["Could not reach the egg"] = true,
	["The egg would not come free"] = true,
	Cancelling = true,
}
v57 = function(p56)
	local v754 = tostring(p56)
	if v134[v754] or string.sub(v754, 1, 9) == "Selected:" then
		v129.Text = v754
	elseif string.sub(v754, 1, 5) == "Error" then
		v129.Text = "Something went wrong"
	elseif v42.Busy then
		v129.Text = "Delivering"
	end
end
local v135 = {}
local v136 = nil
local v138 = function(p57)
	v136 = p57
	for key17, value17 in pairs(v135) do
		local v761 = key17 == p57
		value17.Stroke.Thickness = v761 and 1.5 or 1
		value17.Stroke.Color = v761 and v101.AQUA_STROKE or v101.OFF_BG
		value17.Stroke.Transparency = v761 and 0.2 or 0.45
	end
end
local v139 = function(p58)
	local v768 = v107("TextButton", {
		Name = "EggRow",
		Size = UDim2.new(1, 0, 0, 28),
		BackgroundColor3 = v101.SURF2,
		BackgroundTransparency = v101.SURF2_TRANS,
		BorderSizePixel = 0,
		Text = "",
		AutoButtonColor = false,
		ZIndex = 103,
	})
	v768.Parent = v128
	v108(v768, 11)
	local v770 = v110(v768, v101.OFF_BG, 1, 0.45)
	local v771 = v107("ImageLabel", {
		Size = UDim2.fromOffset(22, 22),
		Position = UDim2.fromOffset(3, 3),
		BackgroundTransparency = 1,
		Image = p58.Icon,
		ScaleType = Enum.ScaleType.Fit,
		ZIndex = 104,
	})
	v771.Parent = v768
	local v773 = v107
	local v774 = "TextLabel"
	local v775 = {
		Size = UDim2.new(1, -30, 0, 13),
		Position = UDim2.fromOffset(28, 2),
		BackgroundTransparency = 1,
		Font = Enum.Font.GothamBold,
		TextSize = 10,
		TextXAlignment = Enum.TextXAlignment.Left,
		TextTruncate = Enum.TextTruncate.AtEnd,
		Text = p58.Name,
	}
	local v776 = v102[math.clamp(p58.Rarity, 1, #v102)] or v101.TEXT
	v775.TextColor3 = v776
	v775.ZIndex = 104
	local v777 = v773(v774, v775)
	v777.Parent = v768
	local v779 = v107("TextLabel", {
		Size = UDim2.new(1, -30, 0, 11),
		Position = UDim2.fromOffset(28, 15),
		BackgroundTransparency = 1,
		Font = Enum.Font.GothamMedium,
		TextSize = 9,
		TextXAlignment = Enum.TextXAlignment.Left,
		Text = v55(p58.Value),
		TextColor3 = v101.OFF_TEXT,
		ZIndex = 104,
	})
	v779.Parent = v768
	v768.MouseButton1Click:Connect(function()
		v138(p58.Uid)
		v57("Selected: " .. p58.Name)
	end)
	v768.MouseEnter:Connect(function()
		v112(v768, 0.14, { BackgroundColor3 = Color3.fromRGB(40, 65, 115), BackgroundTransparency = 0.05 })
		v112(v770, 0.14, { Transparency = 0.3 })
	end)
	v768.MouseLeave:Connect(function()
		v112(v768, 0.14, { BackgroundColor3 = v101.SURF2, BackgroundTransparency = v101.SURF2_TRANS })
		v112(v770, 0.14, { Transparency = 0.45 })
	end)
	return { Frame = v768, Stroke = v770, Icon = v771, Name = v777, Value = v779, Info = p58 }
end
local v142 = function(p59, p60)
	p59.Info = p60
	if p59.Icon.Image ~= p60.Icon then
		p59.Icon.Image = p60.Icon
	end
	if p59.Name.Text ~= p60.Name then
		p59.Name.Text = p60.Name
	end
	local v786 = v102[math.clamp(p60.Rarity, 1, #v102)] or v101.TEXT
	if p59.Name.TextColor3 ~= v786 then
		p59.Name.TextColor3 = v786
	end
	local v787 = v55(p60.Value)
	if p59.Value.Text ~= v787 then
		p59.Value.Text = v787
	end
end
local v143 = function()
	local v790 = {}
	for key18, value18 in ipairs(v52()) do
		v790[#v790 + 1] = v54(value18)
	end
	table.sort(v790, function(p61, p62)
		return p61.Value > p62.Value
	end)
	local v799 = {}
	for key19, value19 in ipairs(v790) do
		v799[value19.Uid] = true
		local v811 = v135[value19.Uid]
		if v811 then
			v142(v811, value19)
			if v811.Frame.LayoutOrder ~= key19 then
				v811.Frame.LayoutOrder = key19
			end
		else
			local v813 = v139(value19)
			v813.Frame.LayoutOrder = key19
			v135[value19.Uid] = v813
		end
	end
	for key20, value20 in pairs(v135) do
		if not v799[key20] then
			value20.Frame:Destroy()
			v135[key20] = nil
		end
	end
	if #v790 == 0 then
		v57("Pick a pet")
	end
end
task.spawn(function()
	while v114.Parent do
		task.wait(4)
		if not v42.Busy then
			pcall(v143)
		end
	end
end)
v130.MouseButton1Click:Connect(function()
	if v42.Busy then
		v42.Cancel = true
		v57("Cancelling")
		return
	end
	if not v136 then
		v57("Pick a pet")
		return
	end
	v100(v136)
end)
task.spawn(function()
	while v114.Parent do
		task.wait(0.25)
		v130.Text = v42.Busy and "CANCEL" or "STEAL"
	end
end)
v143()
v41()
local v146 = {
	{
	find = "You stole an EGG",
	replace = "<font color=\"#ffffff\"><stroke color=\"#000000\" thickness=\"2\" joins=\"round\">You stole an <font color=\"#23272e\">EGG</font> using <font color=\"#4287f5\">Tokinu Hub</font></stroke></font>",
},
}
local v147 = { lt = "<", gt = ">", amp = "&", quot = "\"", apos = "'" }
local v148 = {}
local v151 = function(p63)
	local v819 = p63:gsub("<[^>]->", ""):gsub("&(%a+);", v147):gsub("%s+", " "):match("^%s*(.-)%s*$")
	return v819
end
local v154 = function(p64, p65, p66)
	local v821, v822 = p64:find(p65, 1, true)
	if v821 then
		return p64:sub(1, v821 - 1) .. p66 .. p64:sub(v822 + 1)
	end
	return p66
end
local v155 = function(p67)
	pcall(function()
		local text = p67.Text
		local v831 = v151(text)
		if v831:lower() == "invalid code" then
			p67.Text = v154(text, v831, "this code is invalid boi")
			return
		end
		for key21, value21 in ipairs(v146) do
			if v831 == value21.find then
				if value21.replace:find("<[^>]+>") and not p67.RichText then
					p67.RichText = true
				end
				p67.Text = value21.replace
				return
			end
		end
	end)
end
local v157 = function(p68)
	local v841 = false
	pcall(function()
		v841 = p68:IsA("TextLabel") or p68:IsA("TextButton") or p68:IsA("TextBox")
	end)
	if not v841 or v148[p68] then
		return
	end
	v148[p68] = true
	v155(p68)
	p68:GetPropertyChangedSignal("Text"):Connect(function()
		v155(p68)
	end)
end
for key22, value22 in ipairs(game:GetDescendants()) do
	v157(value22)
end
game.DescendantAdded:Connect(v157)
