-- message_1 -- recovered from its Luraph v15.1 VM (bootstrap replayed, payload lifted and decompiled).
--
-- Complete: all 131 functions of the protected program, no lift errors, no unstructured control flow.
-- The file parses as Luau; it was not run in the game.
--
-- What is and is not original:
--   * control flow, calls, strings, numbers and table contents are the script's own;
--   * local variable, parameter and function names do not survive protection.  The names here are
--     re-derived by devm/renamer.py from how a value is made and used (`Players`, `humanoidClone`,
--     `getHumanoidRootPart`, `dt`): guesses about intent, not the author's names.  What nothing can be
--     said about keeps its number (v32, p4);
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
local config = {
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
local safeRequire = function(p1)
	local ok, result = pcall(function()
		return require(p1())
	end)
	return ok and result or nil
end
local networking = ReplicatedStorage:WaitForChild("Packages"):WaitForChild("Networking")
local EggState = safeRequire(function()
	return ReplicatedStorage.Client.EggState
end)
local Assets = safeRequire(function()
	return ReplicatedStorage.Data.Assets
end)
local Mutations = safeRequire(function()
	return ReplicatedStorage.Shared.Modules.Mutations
end)
local findInNetworking = function(message)
	return networking:FindFirstChild(message)
end
local getHumanoidRootPart = function()
	local character = localPlayer.Character
	return character and character:FindFirstChild("HumanoidRootPart")
end
local v26 = { Original = nil, Clone = nil, Links = {}, Connection = nil, Added = nil }
local getWalkSpeed = function()
	local character2 = localPlayer.Character
	local humanoid = character2 and character2:FindFirstChildOfClass("Humanoid")
	local walkSpeed = humanoid and humanoid.WalkSpeed or 16
	if v26.Original and 0 < v26.Original.Health then
		walkSpeed = math.min(walkSpeed, v26.Original.WalkSpeed)
	end
	local ok2, result2 = pcall(function()
		local leaderstats = localPlayer:FindFirstChild("leaderstats")
		local speed = leaderstats and leaderstats:FindFirstChild("Speed")
		return speed and require(ReplicatedStorage.Shared.Util.TreadmillUtil).SpeedPowerToWalkSpeed(speed.Value) or nil
	end)
	if ok2 and type(result2) == "number" and 0 < result2 then
		walkSpeed = math.min(walkSpeed, result2)
	end
	return walkSpeed
end
local getControls = function(p3)
	pcall(function()
		local playerScripts = localPlayer:FindFirstChild("PlayerScripts")
		local playerModule = playerScripts and playerScripts:FindFirstChild("PlayerModule")
		if playerModule then
			local controls = require(playerModule):GetControls()
			if type(controls) == "table" then
				controls.humanoid = p3
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
	for _, link in ipairs(v26.Links) do
		pcall(function()
			link:Disconnect()
		end)
	end
	table.clear(v26.Links)
end
local seen = {}
seen[Enum.HumanoidStateType.Running] = true
seen[Enum.HumanoidStateType.RunningNoPhysics] = true
seen[Enum.HumanoidStateType.Landed] = true
local v37 = function(humanoid5)
	if not humanoid5 or humanoid5.Health <= 0 or humanoid5.FloorMaterial == Enum.Material.Air then
		return false
	end
	return seen[humanoid5:GetState()] == true
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
	local humanoid2Clone = humanoid2:Clone()
	humanoid2.Parent = nil
	humanoid2Clone.Parent = character3
	workspace.CurrentCamera.CameraSubject = humanoid2Clone
	getControls(humanoid2Clone)
	v32(character3)
	v26.Original = humanoid2
	v26.Clone = humanoid2Clone
	table.insert(v26.Links, humanoid2:GetPropertyChangedSignal("WalkSpeed"):Connect(function()
		if humanoid2Clone.Parent ~= nil then
			humanoid2Clone.WalkSpeed = humanoid2.WalkSpeed
		end
	end))
	local animator = humanoid2:FindFirstChildOfClass("Animator")
	local animator2 = humanoid2Clone:FindFirstChildOfClass("Animator")
	if animator and animator2 then
		table.insert(v26.Links, animator.AnimationPlayed:Connect(function(track)
			local animation = track.Animation
			if not animation or humanoid2Clone.Parent == nil then
				return
			end
			local ok3, result3 = pcall(function()
				return animator2:LoadAnimation(animation)
			end)
			if not ok3 or not result3 then
				return
			end
			pcall(function()
				result3.Priority = track.Priority
				result3.Looped = track.Looped
				result3:Play(0.05, math.max(track.WeightTarget, 0.01), track.Speed)
			end)
			local v243 = nil
			v243 = track.Stopped:Connect(function()
				v243:Disconnect()
				pcall(function()
					result3:Stop(0.1)
				end)
			end)
		end))
	end
	table.insert(v26.Links, humanoid2Clone.Died:Connect(function()
		v33()
		v26.Original = nil
		v26.Clone = nil
		local character4 = localPlayer.Character
		if character4 and humanoid2.Parent == nil then
			humanoid2.Parent = character4
			workspace.CurrentCamera.CameraSubject = humanoid2
			getControls(humanoid2)
		end
		pcall(function()
			humanoid2Clone:Destroy()
		end)
		humanoid2.Health = 0
	end))
end
local v41 = function()
	v40()
	local elapsed9 = 0
	v26.Connection = RunService.Heartbeat:Connect(function(dt)
		elapsed9 += dt
		local character5 = localPlayer.Character
		if (not (v26.Clone and character5 and v26.Clone.Parent == character5) and 0.25 or 3) <= elapsed9 then
			elapsed9 = 0
			v40()
		end
	end)
	v26.Added = localPlayer.CharacterAdded:Connect(function(character10)
		v33()
		v26.Original = nil
		v26.Clone = nil
		task.spawn(function()
			character10:WaitForChild("Humanoid", 10)
			task.wait(1)
			if v26.Connection and localPlayer.Character == character10 then
				v40()
			end
		end)
	end)
end
local state = {
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
if type(EggState) == "table" and type(EggState.CarryChanged) == "table" and type(EggState.CarryChanged.Connect) == "function" then
	EggState.CarryChanged:Connect(function(p9)
		local v274 = type(p9) == "table" and p9.IsCarrying == true
		if v274 and p9.GuardDisabled == true then
			v274 = false
		end
		state.GuessedDrop = false
		if v274 then
			state.HeldSeen = os.clock()
		end
		if v274 and type(p9.Uid) == "string" then
			state.Uid = p9.Uid
			local speedMultiplier = tonumber(p9.SpeedMultiplier)
			if speedMultiplier and 0 < speedMultiplier then
				state.Mult = speedMultiplier
			end
		end
		state.Carrying = v274
	end)
end
pcall(function()
	findInNetworking("RE/EggWorld/FieldEggRedeemVerdict").OnClientEvent:Connect(function()
		state.Delivered = os.clock()
	end)
end)
pcall(function()
	findInNetworking("RE/RigSync/Refresh").OnClientEvent:Connect(function(p10)
		if type(p10) == "table" and p10.Action == "Relocate" then
			state.PulledAt = os.clock()
		end
	end)
end)
local carryFieldEgg = function(p11)
	if type(p11) == "string" and type(EggState) == "table" and type(EggState.CarryFieldEgg) == "function" then
		pcall(EggState.CarryFieldEgg, p11)
	end
end
local dropFieldEgg = function()
	if type(EggState) == "table" and type(EggState.DropFieldEgg) == "function" then
		pcall(EggState.DropFieldEgg, "PlayerRequest")
	end
end
local v49 = function(p12, p13)
	local v290 = nil
	local v291 = p13
	for _, child in ipairs(workspace:GetChildren()) do
		if child.Name == "SmartPromptPart" and child:IsA("BasePart") then
			local carryAreaEgg = child:FindFirstChild("CarryAreaEgg")
			if carryAreaEgg and carryAreaEgg:IsA("ProximityPrompt") then
				local distance = (child.Position - p12).Magnitude
				if distance < v291 then
					v290 = carryAreaEgg
					v291 = distance
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
	for _, descendant in ipairs(v304:GetDescendants()) do
		if descendant:IsA("WeldConstraint") or descendant:IsA("JointInstance") then
			local v313, v314, v315 = pcall(function()
				return descendant.Part0, descendant.Part1
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
		if state.Carrying then
			if v50(state.Uid) then
				state.HeldSeen = os.clock()
			elseif 0.8 < os.clock() - state.HeldSeen then
				state.Carrying = false
				state.GuessedDrop = true
			end
		elseif state.GuessedDrop and v50(state.Uid) then
			local now = os.clock()
			state.GuessedDrop = false
			state.Carrying = true
			state.HeldSeen = now
		end
	end
end)
local askFieldEggSnapshot = function()
	local list = {}
	local ok4, result4 = pcall(function()
		return findInNetworking("RF/EggWorld/AskFieldEggSnapshot"):InvokeServer()
	end)
	local records = ok4 and type(result4) == "table" and result4.Records or nil
	if type(records) ~= "table" then
		return list
	end
	for _, record in pairs(records) do
		if type(record) == "table" and type(record.Uid) == "string" and (record.State == "Slot" or record.State == "Dropped") and typeof(record.BottomCFrame) == "CFrame" then
			list[#list + 1] = record
		end
	end
	return list
end
local v53 = function(p15)
	for _, item in ipairs(askFieldEggSnapshot()) do
		if item.Uid == p15 then
			return item.BottomCFrame.Position
		end
	end
	return nil
end
local v54 = function(p16)
	local directory = type(Assets) == "table" and Assets.Directory or nil
	local v336 = type(directory) == "table" and directory[tostring(p16.AssetCategory)] or nil
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
			local text2 = tostring(icon)
			local v345 = tonumber(text2) and "rbxassetid://" .. text2 or text2
			v337.Icon = v345
		end
		if type(v336.Rarity) == "table" then
			local v348 = tonumber(v336.Rarity.RarityNumber or v336.Rarity.Rank) or 0
			v337.Rarity = v348
		end
		local assetScale = tonumber(p16.AssetScale) or 1
		local v350 = assetScale > 5 and (assetScale / 5) ^ 1.2 * 19.637875755794113 or assetScale ^ 1.85
		local v351 = 1
		if type(Mutations) == "table" and type(Mutations.EarningsFor) == "function" then
			local ok5, result5 = pcall(Mutations.EarningsFor, type(p16.Mutations) == "table" and p16.Mutations or {})
			if ok5 and type(result5) == "number" then
				v351 = result5
			end
		end
		local earningRate = tonumber(v336.EarningRate) or 0
		v337.Value = earningRate * v350 * v351
	end
	return v337
end
local formatText = function(p17)
	for _, item2 in ipairs({ { 1000000000000, "T" }, { 1000000000, "B" }, { 1000000, "M" }, { 1000, "K" } }) do
		if item2[1] <= p17 then
			return string.format("$%.2f%s/s", p17 / item2[1], item2[2])
		end
	end
	return string.format("$%d/s", math.floor(p17 + 0.5))
end
local v57 = function(message2)
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
local count = 0
local v62 = function()
	if typeof(setfpscap) ~= "function" then
		return
	end
	count += 1
	local v383 = count
	task.spawn(function()
		local v386 = true
		local now2 = os.clock()
		while count == v383 and os.clock() - now2 < 60 do
			pcall(setfpscap, v386 and v13[1] or v13[2])
			v386 = not v386
			task.wait(v13[3])
		end
	end)
end
local v65 = function()
	count += 1
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
	local character7Clone = character7:Clone()
	character7.Archivable = archivable
	if character7Clone then
		for _, descendant2 in ipairs(character7Clone:GetDescendants()) do
			if descendant2:IsA("LuaSourceContainer") or descendant2:IsA("Humanoid") then
				pcall(function()
					descendant2:Destroy()
				end)
			elseif descendant2:IsA("BasePart") then
				descendant2.Anchored = true
				descendant2.CanCollide = false
				descendant2.CanTouch = false
				descendant2.CanQuery = false
			end
		end
		character7Clone.Name = "Clone"
		character7Clone.Parent = workspace
		v66 = character7Clone
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
		for _, item3 in ipairs(value8[1]) do
			v430 = v430 and v430:FindFirstChild(item3) or nil
		end
		if v430 and v430:IsA("BasePart") then
			return v430.CFrame:PointToWorldSpace(value8[2])
		end
	end
	return Vector3.new(528.7, 70.57, -364.11)
end
local angles = function(p19)
	local rootPart = getHumanoidRootPart()
	if not rootPart then
		return
	end
	pcall(function()
		rootPart.CFrame = CFrame.new(p19) * CFrame.Angles(0, math.rad(90), 0)
		rootPart.AssemblyLinearVelocity = Vector3.zero
		rootPart.AssemblyAngularVelocity = Vector3.zero
	end)
end
local v76 = {}
local v77 = 0
local v80 = function()
	if os.clock() - v77 < 1 then
		return v76
	end
	v77 = os.clock()
	local list2 = {}
	local v447 = function(instance)
		local v480, v481, v482 = pcall(function()
			if instance:IsA("Model") then
				return instance:GetBoundingBox()
			end
			if instance:IsA("BasePart") then
				return instance.CFrame, instance.Size
			end
		end)
		if v480 and v481 and v482 then
			local v483 = Vector3.new(math.abs(v482.X), 0, math.abs(v482.Z)) * 0.5
			local v484 = (v481 - v481.Position):VectorToWorldSpace(v483)
			local v485 = math.max(math.abs(v484.X), v483.X, v483.Z)
			local v486 = math.max(math.abs(v484.Z), v483.X, v483.Z)
			list2[#list2 + 1] = {
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
		local lowered = string.lower(p21)
		return string.find(lowered, "portal", 1, true) or string.find(lowered, "teleport", 1, true) or string.find(lowered, "mech", 1, true) or string.find(lowered, "arena", 1, true) or string.find(lowered, "scramble", 1, true)
	end
	for _, child2 in ipairs(workspace:GetChildren()) do
		if (child2:IsA("Model") or child2:IsA("BasePart") or child2:IsA("Folder")) and v450(child2.Name) then
			if child2:IsA("Folder") then
				for _, child3 in ipairs(child2:GetChildren()) do
					v447(child3)
				end
			else
				v447(child2)
			end
		end
	end
	local world = workspace:FindFirstChild("World")
	local build = world and world:FindFirstChild("Build")
	if build then
		for _, child4 in ipairs(build:GetChildren()) do
			if v450(child4.Name) then
				for _, child5 in ipairs(child4:GetChildren()) do
					v447(child5)
				end
			end
		end
	end
	v76 = list2
	return list2
end
local v81 = function(p22, p23)
	for _, item4 in ipairs(v80()) do
		local v499 = item4.MinX - 12
		local v500 = item4.MaxX + 12
		local v501 = item4.MinZ - 12
		local v502 = item4.MaxZ + 12
		if not (p22.X >= v499 and p22.X <= v500 and p22.Z >= v501 and p22.Z <= v502) then
			local v504 = 0
			local v505 = 1
			local v506 = true
			for _, item5 in ipairs({ { p22.X, p23.X - p22.X, v499, v500 }, { p22.Z, p23.Z - p22.Z, v501, v502 } }) do
				local v518 = item5[1]
				local v519 = item5[2]
				local v520 = item5[3]
				local v521 = item5[4]
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
	local rootPart2 = getHumanoidRootPart()
	if not rootPart2 then
		return false
	end
	local v531 = "field"
	if rootPart2.Position.X < v526 - 2 and 20 < Vector3.new(rootPart2.Position.X - v527.X, 0, rootPart2.Position.Z - v527.Z).Magnitude then
		v531 = "safe"
	end
	local now3 = os.clock()
	local now4 = os.clock()
	local position = rootPart2.Position
	local v535 = 0
	while true do
		if os.clock() - now3 < 120 and not state.Cancel then
			rootPart2 = getHumanoidRootPart()
			if not rootPart2 then
				break
			end
			local vector = Vector3.new(p25.X - rootPart2.Position.X, 0, p25.Z - rootPart2.Position.Z)
			if v531 ~= "field" or not (vector.Magnitude <= 2.5) then
				local v540 = p25
				if v531 == "safe" then
					if not (Vector3.new(v527.X - rootPart2.Position.X, 0, v527.Z - rootPart2.Position.Z).Magnitude <= 6) then
						v540 = v527
					else
						v531 = "field"
					end
				end
				local v542 = v81(rootPart2.Position, v540)
				local vector2 = Vector3.new(v542.X - rootPart2.Position.X, 0, v542.Z - rootPart2.Position.Z)
				local zero = vector2.Magnitude > 0.01 and vector2.Unit or Vector3.zero
				local v545 = zero * math.min(math.max(getWalkSpeed() * config.SpeedCap, 8), vector2.Magnitude / 0.05)
				pcall(function()
					rootPart2.AssemblyLinearVelocity = Vector3.new(v545.X, rootPart2.AssemblyLinearVelocity.Y, v545.Z)
					if humanoid3 and 0 < zero.Magnitude then
						humanoid3:Move(zero, false)
					end
				end)
				if 1.5 <= os.clock() - now4 then
					if (rootPart2.Position - position).Magnitude < 3 and humanoid3 and 15 < vector.Magnitude then
						pcall(function()
							humanoid3.Jump = true
						end)
					end
					position = rootPart2.Position
					now4 = os.clock()
				end
				if v531 == "field" and vector.Magnitude <= 9 and 0.1 < os.clock() - v535 then
					v535 = os.clock()
					carryFieldEgg(p24)
				end
				RunService.Heartbeat:Wait()
				continue
			end
		end
		rootPart2 = getHumanoidRootPart()
		if rootPart2 then
			pcall(function()
				rootPart2.AssemblyLinearVelocity = Vector3.new(0, rootPart2.AssemblyLinearVelocity.Y, 0)
				if humanoid3 then
					humanoid3:Move(Vector3.zero, false)
				end
			end)
		end
		return state.Carrying or rootPart2 ~= nil and Vector3.new(p25.X - rootPart2.Position.X, 0, p25.Z - rootPart2.Position.Z).Magnitude <= 6
	end
	return false
end
local getPos = function(p26, p27)
	local v553 = workspace:FindFirstChild(p26)
	local areaEggSlotsClient = workspace:FindFirstChild("AreaEggSlotsClient")
	local v557 = v553 or areaEggSlotsClient and areaEggSlotsClient:FindFirstChild(p26)
	if v557 then
		local ok6, result6 = pcall(function()
			return v557:GetPivot().Position
		end)
		if ok6 and result6 then
			local now5 = os.clock()
			p27.Pos = result6
			p27.At = now5
			return result6
		end
	end
	if 0.5 <= os.clock() - p27.At then
		p27.At = os.clock()
		local pos = v53(p26) or p27.Pos
		p27.Pos = pos
	end
	return p27.Pos
end
local v87 = function(p28, p29)
	local v566 = { At = 0 }
	local v569 = 0
	local v570 = 1
	while not state.Carrying and v569 < p29 and not state.Cancel do
		local rootPart3 = getHumanoidRootPart()
		local pos2 = getPos(p28, v566)
		local v574 = math.max(RunService.Heartbeat:Wait(), 0.004166666666666667)
		v569 += v574
		v570 += v574
		if not rootPart3 or not pos2 then
			if config.GrabInterval <= v570 then
				v570 = 0
				task.spawn(carryFieldEgg, p28)
			end
		else
			local position2 = pos2 - rootPart3.Position
			if 2 < position2.Magnitude then
				local v576 = math.max(getWalkSpeed() * config.SpeedCap, 16)
				local v577 = position2 / math.max(0.08, v574)
				if v576 < v577.Magnitude then
					v577 = v577.Unit * v576
				end
				pcall(function()
					rootPart3.AssemblyLinearVelocity = v577 + Vector3.new(0, workspace.Gravity * v574 * 0.5, 0)
					rootPart3.AssemblyAngularVelocity = Vector3.zero
				end)
			end
			if config.GrabInterval <= v570 then
				v570 = 0
				local v579 = v49(pos2 - Vector3.new(0, 3, 0), 10)
				if v579 and typeof(fireproximityprompt) == "function" then
					pcall(function()
						v579.HoldDuration = 0
					end)
					pcall(fireproximityprompt, v579)
				end
				task.spawn(carryFieldEgg, p28)
			end
		end
	end
	local rootPart4 = getHumanoidRootPart()
	if rootPart4 then
		pcall(function()
			rootPart4.AssemblyLinearVelocity = Vector3.zero
		end)
	end
	return state.Carrying and state.Uid == p28
end
local v88 = function(p30)
	if state.Carrying then
		return true
	end
	local rootPart5 = getHumanoidRootPart()
	local v586 = v53(p30)
	if not rootPart5 or not v586 or not (config.RegrabFar < Vector3.new(rootPart5.Position.X - v586.X, 0, rootPart5.Position.Z - v586.Z).Magnitude) then
		v57("The egg fell close, taking it back")
	else
		v57("The egg fell far behind, teleporting onto it")
		angles(v586 + Vector3.new(0, 3, 0))
	end
	return v87(p30, 3)
end
local raycast = function(p31, p32)
	local v588 = p32
	pcall(function()
		local raycastParams = RaycastParams.new()
		raycastParams.FilterType = Enum.RaycastFilterType.Exclude
		raycastParams.FilterDescendantsInstances = { localPlayer.Character }
		raycastParams.IgnoreWater = true
		local raycastResult = workspace:Raycast(p31 + Vector3.new(0, 60, 0), Vector3.new(0, -140, 0), raycastParams)
		if raycastResult and raycastResult.Material ~= Enum.Material.Water and math.abs(raycastResult.Position.Y - p32) < 40 then
			v588 = raycastResult.Position.Y + 3.5
		end
	end)
	return v588
end
local v93 = function(p33)
	local walkSpeed2 = getWalkSpeed()
	local mult = walkSpeed2 * math.min(config.CarryRatio, config.SpeedCap) * state.Mult
	local v596 = mult * 1.5
	local v597 = 5.5 * mult
	local v598 = v596
	if p33 and v597 < p33 then
		v598 = math.min(v596, mult * p33 / (p33 - v597))
	end
	return math.min(math.max(math.min(mult * config.EasyRatio, v598), mult), math.max(walkSpeed2 * config.SpeedCap, mult))
end
local v96 = function(p34, p35)
	local now6 = os.clock()
	local v601 = v74()
	local v602 = p34 - 7
	local height = config.Height
	local v604 = math.clamp(config.ClimbShare, 0.1, 0.9)
	local v605 = height * math.sqrt(1 - v604 * v604) / v604
	local character9 = localPlayer.Character
	local humanoid4 = character9 and character9:FindFirstChildOfClass("Humanoid")
	if humanoid4 then
		humanoid4.PlatformStand = false
	end
	local rootPart6 = getHumanoidRootPart()
	if rootPart6 and character9 and 0.5 < height and rootPart6.Position.Y < v601.Y + height - 2 then
		pcall(function()
			character9:PivotTo(CFrame.new(Vector3.new(rootPart6.Position.X, v601.Y + height, rootPart6.Position.Z)) * rootPart6.CFrame.Rotation)
			rootPart6.AssemblyLinearVelocity = Vector3.zero
			rootPart6.AssemblyAngularVelocity = Vector3.zero
		end)
	end
	local rootPart7 = getHumanoidRootPart()
	local v613 = v93(rootPart7 and Vector3.new(rootPart7.Position.X - v601.X, 0, rootPart7.Position.Z - v601.Z).Magnitude + math.max(0, height) * 2 or nil)
	local now7 = os.clock()
	local v615 = 0
	v57("Safe zone")
	while true do
		if state.Carrying and state.Delivered < now6 and not state.Cancel and v615 < 25 then
			rootPart6 = getHumanoidRootPart()
			if not rootPart6 then
				break
			end
			local now8 = os.clock()
			local v620 = math.max(now8 - now7, 0.004166666666666667)
			now7 = now8
			v615 += v620
			local v621 = rootPart6.Position.X <= v602 + 2
			local v622 = v621 and v601 or Vector3.new(v602, v601.Y, p35)
			if not v621 or not (Vector3.new(v601.X - rootPart6.Position.X, 0, v601.Z - rootPart6.Position.Z).Magnitude < 2) then
				local v623 = v81(rootPart6.Position, v622)
				local vector3 = Vector3.new(v623.X - rootPart6.Position.X, 0, v623.Z - rootPart6.Position.Z)
				local v625 = v621 and 0 or math.max(0, rootPart6.Position.X - v602)
				local v626 = v601.Y + height
				if v621 or v625 <= v605 then
					v626 = v601.Y + height * math.clamp(v625 / math.max(v605, 1), 0, 1)
				end
				local v627 = math.clamp((v626 - rootPart6.Position.Y) / 0.12, -v613 * v604, v613 * v604)
				local v628 = math.sqrt(math.max(v613 * v613 - v627 * v627, 0))
				local zero2 = vector3.Magnitude > 0.01 and vector3.Unit * math.min(v628, vector3.Magnitude / 0.05) or Vector3.zero
				pcall(function()
					rootPart6.AssemblyLinearVelocity = Vector3.new(zero2.X, v627, zero2.Z)
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
		local elapsed = 0
		while elapsed < 2 and state.Delivered < now6 and state.Carrying and not state.Cancel do
			elapsed += RunService.Heartbeat:Wait()
		end
		if state.Carrying and state.Delivered < now6 and not state.Cancel then
			task.wait(0.2)
			dropFieldEgg()
			local elapsed2 = 0
			while state.Delivered < now6 and elapsed2 < 1 do
				elapsed2 += RunService.Heartbeat:Wait()
			end
		end
		return state.Delivered >= now6
	end
	return false
end
local v99 = function(p36)
	local now9 = os.clock()
	local v636, v637 = v71()
	local rootPart8 = getHumanoidRootPart()
	if not rootPart8 then
		return false
	end
	local v639 = math.clamp(rootPart8.Position.Z, -425, -300)
	local vector4 = Vector3.new(v636 + config.LandOffset, v637 + 3.35, v639)
	local v641 = math.max(getWalkSpeed() * config.HopRatio, config.HopMin)
	local hopLift = rootPart8.Position.Y + config.HopLift
	local x = rootPart8.Position.X
	local count2 = 0
	local v645 = v58()
	state.ReleaseCamera = v645
	pcall(v68)
	v62()
	while vector4.X < x - v641 and not state.Cancel do
		if state.Carrying then
			local v647 = x - v641
			v57(string.format("Instant TP: hopping home, X %d", math.floor(v647)))
			local pulledAt = state.PulledAt
			local elapsed3 = 0
			while elapsed3 < config.HopGap do
				angles(Vector3.new(v647, hopLift, v639))
				elapsed3 += RunService.Heartbeat:Wait()
			end
			local rootPart9 = getHumanoidRootPart()
			if not (state.PulledAt > pulledAt or rootPart9 ~= nil and (rootPart9.Position.X - v647 > 10 or rootPart9.AssemblyLinearVelocity.Magnitude > 150)) or not state.Carrying or state.Cancel then
				x = v647
				config.HopGap = math.max(config.HopGapMin, config.HopGap - 0.01)
			else
				count2 += 1
				config.HopGap = math.min(config.HopGapMax, config.HopGap + 0.02)
				if config.HopRetries < count2 then
					break
				end
				v57(string.format("Instant TP: pulled back, retry %d/%d", count2, config.HopRetries))
				local elapsed4 = 0
				while elapsed4 < 0.12 and not state.Cancel do
					angles(Vector3.new(x, hopLift, v639))
					elapsed4 += RunService.Heartbeat:Wait()
				end
			end
		else
			if not v88(p36) then
				break
			end
			local rootPart10 = getHumanoidRootPart()
			if rootPart10 then
				x = math.min(x, rootPart10.Position.X)
			end
		end
	end
	v57("Instant TP: landing next to the line")
	local vector5 = Vector3.new(vector4.X, raycast(vector4, vector4.Y), vector4.Z)
	local v658 = function()
		local rootPart11 = getHumanoidRootPart()
		if not rootPart11 or not (rootPart11.Position.X <= vector5.X + 1) or not (vector5.Y - 25 < rootPart11.Position.Y) then
			angles(vector5)
			return
		end
		pcall(function()
			rootPart11.AssemblyLinearVelocity = Vector3.zero
		end)
	end
	v658()
	for i = 1, 3 do
		local elapsed5 = 0
		while elapsed5 < config.LandSettle and not state.Cancel do
			elapsed5 += RunService.Heartbeat:Wait()
		end
		local rootPart12 = getHumanoidRootPart()
		if not state.Carrying or not rootPart12 or not (12 < rootPart12.Position.X - vector5.X) and not (rootPart12.Position.Y < vector5.Y - 25) then
			break
		end
		v57("Instant TP: landing retry " .. i)
		v658()
	end
	v67()
	if state.Carrying and not state.Cancel then
		local elapsed6 = 0
		while elapsed6 < config.DropDelay do
			elapsed6 += RunService.Heartbeat:Wait()
		end
		v57("Instant TP: dropping the egg")
		dropFieldEgg()
		local elapsed7 = 0
		while state.Carrying and elapsed7 < 1 and not state.Cancel do
			elapsed7 += RunService.Heartbeat:Wait()
		end
		local rootPart13 = getHumanoidRootPart()
		if rootPart13 then
			pcall(function()
				rootPart13.AssemblyLinearVelocity = Vector3.zero
				rootPart13.AssemblyAngularVelocity = Vector3.zero
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
	if state.Carrying then
		v666 = v96(v636, v639)
	end
	v65()
	return v666 or state.Delivered >= now9
end
local v100 = function(p37)
	if state.Busy then
		return
	end
	state.Busy = true
	state.Cancel = false
	task.spawn(function()
		local ok7, result7 = pcall(function()
			v57("Looking for the egg")
			local v688 = v53(p37)
			if not v688 then
				v57("That egg is gone")
				return
			end
			if state.Carrying and state.Uid ~= p37 then
				dropFieldEgg()
				local elapsed8 = 0
				while state.Carrying and elapsed8 < 1 do
					elapsed8 += RunService.Heartbeat:Wait()
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
		if state.ReleaseCamera then
			pcall(state.ReleaseCamera)
			state.ReleaseCamera = nil
		end
		if not ok7 then
			v57("Error: " .. tostring(result7))
		end
		state.Busy = false
	end)
end
local colors = {
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
local getCoreGui = function()
	if typeof(gethui) == "function" then
		local ok8, result8 = pcall(gethui)
		if ok8 and typeof(result8) == "Instance" then
			return result8
		end
	end
	return CoreGui
end
local create = function(className, props)
	local v698 = Instance.new(className)
	for key16, value16 in pairs(props or {}) do
		v698[key16] = value16
	end
	return v698
end
local v108 = function(p40, p41)
	local v707 = create
	local v708 = "UICorner"
	local v709 = {}
	v709.CornerRadius = UDim.new(0, p41 or 6)
	local v713 = v707(v708, v709)
	v713.Parent = p40
	return v713
end
local v110 = function(p42, p43, p44, p45)
	local v715 = create
	local v716 = "UIStroke"
	local v717 = { Color = p43 }
	v717.Thickness = p44 or 1
	v717.Transparency = p45 or 0
	v717.ApplyStrokeMode = Enum.ApplyStrokeMode.Border
	local v720 = v715(v716, v717)
	v720.Parent = p42
	return v720
end
local tween = function(p46, p47, p48, p49, p50)
	local v724 = TweenService
	v724:Create(p46, TweenInfo.new(p47 or 0.2, p49 or Enum.EasingStyle.Quint, p50 or Enum.EasingDirection.Out), p48):Play()
end
local tokinuEgg = getCoreGui():FindFirstChild("TokinuEgg")
if tokinuEgg then
	tokinuEgg:Destroy()
end
local tokinuEggGui = create("ScreenGui", {
	Name = "TokinuEgg",
	ResetOnSpawn = false,
	ZIndexBehavior = Enum.ZIndexBehavior.Sibling,
	IgnoreGuiInset = true,
	DisplayOrder = 999,
})
tokinuEggGui.Parent = getCoreGui()
local tokinuEggFrame = create("Frame", {
	Name = "TokinuEggFrame",
	Size = UDim2.fromOffset(240, 320),
	Position = UDim2.new(0.5, -120, 0.5, -160),
	BackgroundColor3 = colors.BG,
	BackgroundTransparency = colors.BG_TRANS,
	BorderSizePixel = 0,
	ClipsDescendants = false,
	Active = true,
})
tokinuEggFrame.Parent = tokinuEggGui
v108(tokinuEggFrame, 18)
v110(tokinuEggFrame, colors.AQUA_STROKE, 1.2, 0.3)
create("ImageLabel", {
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
}).Parent = tokinuEggFrame
local headerFrame = create("Frame", {
	Name = "Header",
	Size = UDim2.new(1, 0, 0, 44),
	BackgroundTransparency = 1,
	ZIndex = 101,
	Active = true,
})
headerFrame.Parent = tokinuEggFrame
local titleLabel = create("TextLabel", {
	Name = "Title",
	Size = UDim2.new(1, -28, 0, 24),
	Position = UDim2.new(0, 14, 0, 10),
	ZIndex = 102,
	BackgroundTransparency = 1,
	Text = "TOKINU | SAE",
	Font = Enum.Font.GothamBlack,
	TextSize = 16,
	TextColor3 = colors.TEXT,
	TextXAlignment = Enum.TextXAlignment.Center,
})
titleLabel.Parent = headerFrame
create("UIGradient", { Color = ColorSequence.new(Color3.new(1, 1, 1), Color3.fromRGB(150, 200, 255)) }).Parent = titleLabel
local dividerFrame = create("Frame", {
	Name = "Divider",
	AnchorPoint = Vector2.new(0.5, 0),
	Position = UDim2.new(0.5, 0, 0, 38),
	Size = UDim2.new(0, 124, 0, 1),
	BackgroundColor3 = Color3.fromRGB(150, 200, 255),
	BackgroundTransparency = 0.2,
	BorderSizePixel = 0,
	ZIndex = 101,
})
dividerFrame.Parent = tokinuEggFrame
create("UIGradient", { Color = ColorSequence.new(Color3.new(1, 1, 1), Color3.fromRGB(165, 175, 195)), Rotation = 90 }).Parent = tokinuEggFrame
create("UIGradient", {
	Transparency = NumberSequence.new({
	NumberSequenceKeypoint.new(0, 1),
	NumberSequenceKeypoint.new(0.2, 0.1),
	NumberSequenceKeypoint.new(0.8, 0.1),
	NumberSequenceKeypoint.new(1, 1),
}),
}).Parent = dividerFrame
;(function(p51, p52)
	local v733 = nil
	local v734 = nil
	local position3 = nil
	local position4 = nil
	local v737 = function(p53)
		local v739 = p53 - position3
		p51.Position = UDim2.new(position4.X.Scale, position4.X.Offset + v739.X, position4.Y.Scale, position4.Y.Offset + v739.Y)
	end
	p52.InputBegan:Connect(function(input)
		if input.UserInputType == Enum.UserInputType.MouseButton1 or input.UserInputType == Enum.UserInputType.Touch then
			v733 = true
			v734 = input
			position3 = input.Position
			position4 = p51.Position
			input.Changed:Connect(function()
				if input.UserInputState == Enum.UserInputState.End then
					v733 = false
				end
			end)
		end
	end)
	UserInputService.InputChanged:Connect(function(input2)
		if v733 and (input2.UserInputType == Enum.UserInputType.MouseMovement or input2.UserInputType == Enum.UserInputType.Touch) then
			v737(input2.Position)
		end
	end)
end)(tokinuEggFrame, headerFrame)
local foldButton = create("TextButton", {
	Name = "FoldButton",
	AutoButtonColor = false,
	Size = UDim2.fromOffset(22, 22),
	Position = UDim2.new(1, -32, 0, 11),
	BackgroundColor3 = colors.OFF_BG,
	BackgroundTransparency = 0.1,
	BorderSizePixel = 0,
	Text = "-",
	Font = Enum.Font.GothamBold,
	TextSize = 16,
	TextColor3 = colors.TEXT,
	ZIndex = 105,
})
foldButton.Parent = headerFrame
v108(foldButton, 7)
local v122 = v110(foldButton, colors.AQUA_STROKE, 1, 0.5)
foldButton.MouseEnter:Connect(function()
	tween(foldButton, 0.14, { BackgroundColor3 = Color3.fromRGB(40, 65, 115), BackgroundTransparency = 0.05 })
	tween(v122, 0.14, { Transparency = 0.3 })
end)
foldButton.MouseLeave:Connect(function()
	tween(foldButton, 0.14, { BackgroundColor3 = colors.OFF_BG, BackgroundTransparency = 0.1 })
	tween(v122, 0.14, { Transparency = 0.5 })
end)
local contentBoxFrame = create("Frame", {
	Name = "ContentBox",
	Size = UDim2.new(1, -20, 1, -52),
	Position = UDim2.fromOffset(10, 48),
	BackgroundColor3 = colors.SURF,
	BackgroundTransparency = colors.SURF_TRANS,
	BorderSizePixel = 0,
	ZIndex = 101,
})
contentBoxFrame.Parent = tokinuEggFrame
v108(contentBoxFrame, 16)
v110(contentBoxFrame, colors.AQUA_STROKE, 1, 0.4)
local innerFrame = create("Frame", {
	Name = "Inner",
	Size = UDim2.new(1, -10, 1, -10),
	Position = UDim2.fromOffset(5, 5),
	BackgroundTransparency = 1,
	ZIndex = 102,
})
innerFrame.Parent = contentBoxFrame
local v126 = false
local size = nil
foldButton.MouseButton1Click:Connect(function()
	v126 = not v126
	if v126 then
		contentBoxFrame.Visible = false
		size = tokinuEggFrame.Size
		tokinuEggFrame.Size = UDim2.fromOffset(240, 44)
		foldButton.Text = "+"
	else
		contentBoxFrame.Visible = true
		if size then
			tokinuEggFrame.Size = size
			size = nil
		end
		foldButton.Text = "-"
	end
end)
local eggListScroll = create("ScrollingFrame", {
	Name = "EggList",
	Size = UDim2.new(1, 0, 1, -58),
	Position = UDim2.fromOffset(0, 0),
	BackgroundTransparency = 1,
	BorderSizePixel = 0,
	ScrollBarThickness = 3,
	ScrollBarImageColor3 = colors.AQUA_STROKE,
	CanvasSize = UDim2.new(),
	AutomaticCanvasSize = Enum.AutomaticSize.Y,
	ZIndex = 102,
})
eggListScroll.Parent = innerFrame
create("UIListLayout", { Padding = UDim.new(0, 6), SortOrder = Enum.SortOrder.LayoutOrder }).Parent = eggListScroll
create("UIPadding", {
	PaddingTop = UDim.new(0, 5),
	PaddingBottom = UDim.new(0, 5),
	PaddingLeft = UDim.new(0, 5),
	PaddingRight = UDim.new(0, 6),
}).Parent = eggListScroll
local statusLabel = create("TextLabel", {
	Name = "Status",
	Size = UDim2.new(1, 0, 0, 14),
	Position = UDim2.new(0, 0, 1, -54),
	BackgroundTransparency = 1,
	Font = Enum.Font.Gotham,
	TextSize = 9,
	TextColor3 = colors.OFF_TEXT,
	TextXAlignment = Enum.TextXAlignment.Left,
	TextTruncate = Enum.TextTruncate.AtEnd,
	Text = "Pick a pet",
	ZIndex = 103,
})
statusLabel.Parent = innerFrame
local stealButton = create("TextButton", {
	Name = "StealButton",
	Size = UDim2.new(1, 0, 0, 34),
	Position = UDim2.new(0, 0, 1, -34),
	BackgroundColor3 = colors.OFF_BG,
	BackgroundTransparency = 0.1,
	BorderSizePixel = 0,
	Font = Enum.Font.GothamBlack,
	TextSize = 12,
	TextColor3 = colors.TEXT,
	Text = "STEAL",
	AutoButtonColor = false,
	ZIndex = 104,
})
stealButton.Parent = innerFrame
v108(stealButton, 11)
local v131 = v110(stealButton, colors.AQUA_STROKE, 1, 0.5)
stealButton.MouseEnter:Connect(function()
	tween(stealButton, 0.14, { BackgroundColor3 = Color3.fromRGB(40, 65, 115), BackgroundTransparency = 0.05 })
	tween(v131, 0.14, { Transparency = 0.3 })
end)
stealButton.MouseLeave:Connect(function()
	tween(stealButton, 0.14, { BackgroundColor3 = colors.OFF_BG, BackgroundTransparency = 0.1 })
	tween(v131, 0.14, { Transparency = 0.5 })
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
	local text3 = tostring(p56)
	if v134[text3] or string.sub(text3, 1, 9) == "Selected:" then
		statusLabel.Text = text3
	elseif string.sub(text3, 1, 5) == "Error" then
		statusLabel.Text = "Something went wrong"
	elseif state.Busy then
		statusLabel.Text = "Delivering"
	end
end
local v135 = {}
local v136 = nil
local v138 = function(p57)
	v136 = p57
	for key17, value17 in pairs(v135) do
		local v761 = key17 == p57
		value17.Stroke.Thickness = v761 and 1.5 or 1
		value17.Stroke.Color = v761 and colors.AQUA_STROKE or colors.OFF_BG
		value17.Stroke.Transparency = v761 and 0.2 or 0.45
	end
end
local v139 = function(p58)
	local eggRowButton = create("TextButton", {
		Name = "EggRow",
		Size = UDim2.new(1, 0, 0, 28),
		BackgroundColor3 = colors.SURF2,
		BackgroundTransparency = colors.SURF2_TRANS,
		BorderSizePixel = 0,
		Text = "",
		AutoButtonColor = false,
		ZIndex = 103,
	})
	eggRowButton.Parent = eggListScroll
	v108(eggRowButton, 11)
	local v770 = v110(eggRowButton, colors.OFF_BG, 1, 0.45)
	local imageLabel = create("ImageLabel", {
		Size = UDim2.fromOffset(22, 22),
		Position = UDim2.fromOffset(3, 3),
		BackgroundTransparency = 1,
		Image = p58.Icon,
		ScaleType = Enum.ScaleType.Fit,
		ZIndex = 104,
	})
	imageLabel.Parent = eggRowButton
	local v773 = create
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
	local text4 = v102[math.clamp(p58.Rarity, 1, #v102)] or colors.TEXT
	v775.TextColor3 = text4
	v775.ZIndex = 104
	local v777 = v773(v774, v775)
	v777.Parent = eggRowButton
	local textLabel = create("TextLabel", {
		Size = UDim2.new(1, -30, 0, 11),
		Position = UDim2.fromOffset(28, 15),
		BackgroundTransparency = 1,
		Font = Enum.Font.GothamMedium,
		TextSize = 9,
		TextXAlignment = Enum.TextXAlignment.Left,
		Text = formatText(p58.Value),
		TextColor3 = colors.OFF_TEXT,
		ZIndex = 104,
	})
	textLabel.Parent = eggRowButton
	eggRowButton.MouseButton1Click:Connect(function()
		v138(p58.Uid)
		v57("Selected: " .. p58.Name)
	end)
	eggRowButton.MouseEnter:Connect(function()
		tween(eggRowButton, 0.14, { BackgroundColor3 = Color3.fromRGB(40, 65, 115), BackgroundTransparency = 0.05 })
		tween(v770, 0.14, { Transparency = 0.3 })
	end)
	eggRowButton.MouseLeave:Connect(function()
		tween(eggRowButton, 0.14, { BackgroundColor3 = colors.SURF2, BackgroundTransparency = colors.SURF2_TRANS })
		tween(v770, 0.14, { Transparency = 0.45 })
	end)
	return { Frame = eggRowButton, Stroke = v770, Icon = imageLabel, Name = v777, Value = textLabel, Info = p58 }
end
local v142 = function(p59, p60)
	p59.Info = p60
	if p59.Icon.Image ~= p60.Icon then
		p59.Icon.Image = p60.Icon
	end
	if p59.Name.Text ~= p60.Name then
		p59.Name.Text = p60.Name
	end
	local text5 = v102[math.clamp(p60.Rarity, 1, #v102)] or colors.TEXT
	if p59.Name.TextColor3 ~= text5 then
		p59.Name.TextColor3 = text5
	end
	local v787 = formatText(p60.Value)
	if p59.Value.Text ~= v787 then
		p59.Value.Text = v787
	end
end
local v143 = function()
	local list3 = {}
	for _, item6 in ipairs(askFieldEggSnapshot()) do
		list3[#list3 + 1] = v54(item6)
	end
	table.sort(list3, function(a, b)
		return a.Value > b.Value
	end)
	local seen2 = {}
	for index, item7 in ipairs(list3) do
		seen2[item7.Uid] = true
		local v811 = v135[item7.Uid]
		if v811 then
			v142(v811, item7)
			if v811.Frame.LayoutOrder ~= index then
				v811.Frame.LayoutOrder = index
			end
		else
			local v813 = v139(item7)
			v813.Frame.LayoutOrder = index
			v135[item7.Uid] = v813
		end
	end
	for key20, value20 in pairs(v135) do
		if not seen2[key20] then
			value20.Frame:Destroy()
			v135[key20] = nil
		end
	end
	if #list3 == 0 then
		v57("Pick a pet")
	end
end
task.spawn(function()
	while tokinuEggGui.Parent do
		task.wait(4)
		if not state.Busy then
			pcall(v143)
		end
	end
end)
stealButton.MouseButton1Click:Connect(function()
	if state.Busy then
		state.Cancel = true
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
	while tokinuEggGui.Parent do
		task.wait(0.25)
		stealButton.Text = state.Busy and "CANCEL" or "STEAL"
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
local seen3 = {}
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
		for _, item8 in ipairs(v146) do
			if v831 == item8.find then
				if item8.replace:find("<[^>]+>") and not p67.RichText then
					p67.RichText = true
				end
				p67.Text = item8.replace
				return
			end
		end
	end)
end
local v157 = function(instance2)
	local v841 = false
	pcall(function()
		v841 = instance2:IsA("TextLabel") or instance2:IsA("TextButton") or instance2:IsA("TextBox")
	end)
	if not v841 or seen3[instance2] then
		return
	end
	seen3[instance2] = true
	v155(instance2)
	instance2:GetPropertyChangedSignal("Text"):Connect(function()
		v155(instance2)
	end)
end
for _, descendant3 in ipairs(game:GetDescendants()) do
	v157(descendant3)
end
game.DescendantAdded:Connect(v157)
