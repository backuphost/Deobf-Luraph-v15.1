-- message_1 -- recovered from its Luraph v15.1 VM (bootstrap replayed, payload lifted and decompiled).
--
-- Complete: all 131 functions of the protected program (including three it defines but never calls), no lift errors, no unstructured control flow.
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
			local v248 = nil
			v248 = track.Stopped:Connect(function()
				v248:Disconnect()
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
local v42 = function()
	v33()
	local character5 = localPlayer.Character
	local original = v26.Original
	local clone = v26.Clone
	v26.Original = nil
	v26.Clone = nil
	if original and clone and character5 and original.Parent == nil and clone.Parent == character5 then
		original.Parent = character5
		workspace.CurrentCamera.CameraSubject = original
		getControls(original)
		pcall(function()
			clone:Destroy()
		end)
		v32(character5)
	end
end
local v43 = function()
	v40()
	local elapsed9 = 0
	v26.Connection = RunService.Heartbeat:Connect(function(dt)
		elapsed9 += dt
		local character6 = localPlayer.Character
		if (not (v26.Clone and character6 and v26.Clone.Parent == character6) and 0.25 or 3) <= elapsed9 then
			elapsed9 = 0
			v40()
		end
	end)
	v26.Added = localPlayer.CharacterAdded:Connect(function(character11)
		v33()
		v26.Original = nil
		v26.Clone = nil
		task.spawn(function()
			character11:WaitForChild("Humanoid", 10)
			task.wait(1)
			if v26.Connection and localPlayer.Character == character11 then
				v40()
			end
		end)
	end)
end
local v46 = function()
	if v26.Connection then
		v26.Connection:Disconnect()
		v26.Connection = nil
	end
	if v26.Added then
		v26.Added:Disconnect()
		v26.Added = nil
	end
	v42()
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
		local v291 = type(p9) == "table" and p9.IsCarrying == true
		if v291 and p9.GuardDisabled == true then
			v291 = false
		end
		state.GuessedDrop = false
		if v291 then
			state.HeldSeen = os.clock()
		end
		if v291 and type(p9.Uid) == "string" then
			state.Uid = p9.Uid
			local speedMultiplier = tonumber(p9.SpeedMultiplier)
			if speedMultiplier and 0 < speedMultiplier then
				state.Mult = speedMultiplier
			end
		end
		state.Carrying = v291
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
local v54 = function(p12, p13)
	local v307 = nil
	local v308 = p13
	for _, child in ipairs(workspace:GetChildren()) do
		if child.Name == "SmartPromptPart" and child:IsA("BasePart") then
			local carryAreaEgg = child:FindFirstChild("CarryAreaEgg")
			if carryAreaEgg and carryAreaEgg:IsA("ProximityPrompt") then
				local distance = (child.Position - p12).Magnitude
				if distance < v308 then
					v307 = carryAreaEgg
					v308 = distance
				end
			end
		end
	end
	return v307
end
local v55 = function(p14)
	local character7 = localPlayer.Character
	if type(p14) ~= "string" or not character7 then
		return false
	end
	local v321 = workspace:FindFirstChild(p14)
	if not v321 then
		return false
	end
	for _, descendant in ipairs(v321:GetDescendants()) do
		if descendant:IsA("WeldConstraint") or descendant:IsA("JointInstance") then
			local v330, v331, v332 = pcall(function()
				return descendant.Part0, descendant.Part1
			end)
			if v330 and (v331 and v331:IsDescendantOf(character7) or v332 and v332:IsDescendantOf(character7)) then
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
			if v55(state.Uid) then
				state.HeldSeen = os.clock()
			elseif 0.8 < os.clock() - state.HeldSeen then
				state.Carrying = false
				state.GuessedDrop = true
			end
		elseif state.GuessedDrop and v55(state.Uid) then
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
local v58 = function(p15)
	for _, item in ipairs(askFieldEggSnapshot()) do
		if item.Uid == p15 then
			return item.BottomCFrame.Position
		end
	end
	return nil
end
local v59 = function(p16)
	local directory = type(Assets) == "table" and Assets.Directory or nil
	local v353 = type(directory) == "table" and directory[tostring(p16.AssetCategory)] or nil
	local v354 = {
		Uid = p16.Uid,
		Category = tostring(p16.AssetCategory),
		Name = tostring(p16.AssetCategory),
		Icon = "",
		Rarity = 0,
		Value = 0,
	}
	if type(v353) == "table" then
		v354.Name = tostring(v353.DisplayName or p16.AssetCategory)
		local icon = v353.Icon
		if icon ~= nil and tostring(icon) ~= "" then
			local text2 = tostring(icon)
			local v362 = tonumber(text2) and "rbxassetid://" .. text2 or text2
			v354.Icon = v362
		end
		if type(v353.Rarity) == "table" then
			local v365 = tonumber(v353.Rarity.RarityNumber or v353.Rarity.Rank) or 0
			v354.Rarity = v365
		end
		local assetScale = tonumber(p16.AssetScale) or 1
		local v367 = assetScale > 5 and (assetScale / 5) ^ 1.2 * 19.637875755794113 or assetScale ^ 1.85
		local v368 = 1
		if type(Mutations) == "table" and type(Mutations.EarningsFor) == "function" then
			local ok5, result5 = pcall(Mutations.EarningsFor, type(p16.Mutations) == "table" and p16.Mutations or {})
			if ok5 and type(result5) == "number" then
				v368 = result5
			end
		end
		local earningRate = tonumber(v353.EarningRate) or 0
		v354.Value = earningRate * v367 * v368
	end
	return v354
end
local formatText = function(p17)
	for _, item2 in ipairs({ { 1000000000000, "T" }, { 1000000000, "B" }, { 1000000, "M" }, { 1000, "K" } }) do
		if item2[1] <= p17 then
			return string.format("$%.2f%s/s", p17 / item2[1], item2[2])
		end
	end
	return string.format("$%d/s", math.floor(p17 + 0.5))
end
local v62 = function(message2)
end
local v63 = function()
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
local v67 = function()
	if typeof(setfpscap) ~= "function" then
		return
	end
	count += 1
	local v400 = count
	task.spawn(function()
		local v403 = true
		local now2 = os.clock()
		while count == v400 and os.clock() - now2 < 60 do
			pcall(setfpscap, v403 and v13[1] or v13[2])
			v403 = not v403
			task.wait(v13[3])
		end
	end)
end
local v70 = function()
	count += 1
	if typeof(setfpscap) == "function" then
		pcall(setfpscap, 240)
	end
end
local v71 = nil
local v72 = function()
	local v412 = v71
	v71 = nil
	if v412 then
		pcall(function()
			v412:Destroy()
		end)
	end
end
local v73 = function()
	v72()
	local character8 = localPlayer.Character
	if not character8 then
		return
	end
	local archivable = character8.Archivable
	character8.Archivable = true
	local character8Clone = character8:Clone()
	character8.Archivable = archivable
	if character8Clone then
		for _, descendant2 in ipairs(character8Clone:GetDescendants()) do
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
		character8Clone.Name = "Clone"
		character8Clone.Parent = workspace
		v71 = character8Clone
	end
end
local v76 = function()
	local objects = workspace:FindFirstChild("World") or workspace:FindFirstChild("__OBJECTS")
	local areas = objects and objects:FindFirstChild("Areas")
	local separationLine = areas and areas:FindFirstChild("SeparationLine")
	local v436 = separationLine and separationLine:IsA("BasePart")
	return v436 and separationLine.Position.X or 552.2, v436 and separationLine.Position.Y or 67.67
end
local v79 = function()
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
		local v447 = workspace
		for _, item3 in ipairs(value8[1]) do
			v447 = v447 and v447:FindFirstChild(item3) or nil
		end
		if v447 and v447:IsA("BasePart") then
			return v447.CFrame:PointToWorldSpace(value8[2])
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
local v81 = {}
local v82 = 0
local v85 = function()
	if os.clock() - v82 < 1 then
		return v81
	end
	v82 = os.clock()
	local list2 = {}
	local v464 = function(instance)
		local v497, v498, v499 = pcall(function()
			if instance:IsA("Model") then
				return instance:GetBoundingBox()
			end
			if instance:IsA("BasePart") then
				return instance.CFrame, instance.Size
			end
		end)
		if v497 and v498 and v499 then
			local v500 = Vector3.new(math.abs(v499.X), 0, math.abs(v499.Z)) * 0.5
			local v501 = (v498 - v498.Position):VectorToWorldSpace(v500)
			local v502 = math.max(math.abs(v501.X), v500.X, v500.Z)
			local v503 = math.max(math.abs(v501.Z), v500.X, v500.Z)
			list2[#list2 + 1] = {
				MinX = v498.Position.X - v502,
				MaxX = v498.Position.X + v502,
				MinZ = v498.Position.Z - v503,
				MaxZ = v498.Position.Z + v503,
			}
		end
	end
	local v467 = function(p21)
		if p21 == "ScrambleLocalVisuals" or p21 == "DrScrambleEvent" then
			return false
		end
		local lowered = string.lower(p21)
		return string.find(lowered, "portal", 1, true) or string.find(lowered, "teleport", 1, true) or string.find(lowered, "mech", 1, true) or string.find(lowered, "arena", 1, true) or string.find(lowered, "scramble", 1, true)
	end
	for _, child2 in ipairs(workspace:GetChildren()) do
		if (child2:IsA("Model") or child2:IsA("BasePart") or child2:IsA("Folder")) and v467(child2.Name) then
			if child2:IsA("Folder") then
				for _, child3 in ipairs(child2:GetChildren()) do
					v464(child3)
				end
			else
				v464(child2)
			end
		end
	end
	local world = workspace:FindFirstChild("World")
	local build = world and world:FindFirstChild("Build")
	if build then
		for _, child4 in ipairs(build:GetChildren()) do
			if v467(child4.Name) then
				for _, child5 in ipairs(child4:GetChildren()) do
					v464(child5)
				end
			end
		end
	end
	v81 = list2
	return list2
end
local v86 = function(p22, p23)
	for _, item4 in ipairs(v85()) do
		local v516 = item4.MinX - 12
		local v517 = item4.MaxX + 12
		local v518 = item4.MinZ - 12
		local v519 = item4.MaxZ + 12
		if not (p22.X >= v516 and p22.X <= v517 and p22.Z >= v518 and p22.Z <= v519) then
			local v521 = 0
			local v522 = 1
			local v523 = true
			for _, item5 in ipairs({ { p22.X, p23.X - p22.X, v516, v517 }, { p22.Z, p23.Z - p22.Z, v518, v519 } }) do
				local v535 = item5[1]
				local v536 = item5[2]
				local v537 = item5[3]
				local v538 = item5[4]
				if math.abs(v536) < 1e-06 then
					if v535 < v537 or v538 < v535 then
						v523 = false
					end
				else
					local v539 = (v537 - v535) / v536
					local v540 = (v538 - v535) / v536
					if v540 < v539 then
						local v541 = v539
						v539 = v540
						v540 = v541
					end
					v521 = math.max(v521, v539)
					v522 = math.min(v522, v540)
					if v522 < v521 then
						v523 = false
					end
				end
			end
			if v523 then
				local v529 = v518 - 2
				local v530 = v519 + 2
				local v531 = math.abs(p22.Z - v529) <= math.abs(p22.Z - v530) and v529 or v530
				if v531 < -440 or -290 < v531 then
					v531 = v531 == v529 and v530 or v529
				end
				local v533 = math.abs(p22.X - v516) <= math.abs(p22.X - v517) and v516 or v517
				if math.abs(p22.Z - v531) < 3 then
					v533 = math.abs(p23.X - v516) <= math.abs(p23.X - v517) and v516 or v517
				end
				return Vector3.new(v533, p23.Y, v531)
			end
		end
	end
	return p23
end
local v88 = function(p24, p25)
	local v543 = v76()
	local v544 = v79()
	local character9 = localPlayer.Character
	local humanoid3 = character9 and character9:FindFirstChildOfClass("Humanoid")
	if humanoid3 then
		humanoid3.PlatformStand = false
		if character9:FindFirstChildWhichIsA("Tool") then
			pcall(function()
				humanoid3:UnequipTools()
			end)
		end
	end
	local rootPart2 = getHumanoidRootPart()
	if not rootPart2 then
		return false
	end
	local v548 = "field"
	if rootPart2.Position.X < v543 - 2 and 20 < Vector3.new(rootPart2.Position.X - v544.X, 0, rootPart2.Position.Z - v544.Z).Magnitude then
		v548 = "safe"
	end
	local now3 = os.clock()
	local now4 = os.clock()
	local position = rootPart2.Position
	local v552 = 0
	while true do
		if os.clock() - now3 < 120 and not state.Cancel then
			rootPart2 = getHumanoidRootPart()
			if not rootPart2 then
				break
			end
			local vector = Vector3.new(p25.X - rootPart2.Position.X, 0, p25.Z - rootPart2.Position.Z)
			if v548 ~= "field" or not (vector.Magnitude <= 2.5) then
				local v557 = p25
				if v548 == "safe" then
					if not (Vector3.new(v544.X - rootPart2.Position.X, 0, v544.Z - rootPart2.Position.Z).Magnitude <= 6) then
						v557 = v544
					else
						v548 = "field"
					end
				end
				local v559 = v86(rootPart2.Position, v557)
				local vector2 = Vector3.new(v559.X - rootPart2.Position.X, 0, v559.Z - rootPart2.Position.Z)
				local zero = vector2.Magnitude > 0.01 and vector2.Unit or Vector3.zero
				local v562 = zero * math.min(math.max(getWalkSpeed() * config.SpeedCap, 8), vector2.Magnitude / 0.05)
				pcall(function()
					rootPart2.AssemblyLinearVelocity = Vector3.new(v562.X, rootPart2.AssemblyLinearVelocity.Y, v562.Z)
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
				if v548 == "field" and vector.Magnitude <= 9 and 0.1 < os.clock() - v552 then
					v552 = os.clock()
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
	local v570 = workspace:FindFirstChild(p26)
	local areaEggSlotsClient = workspace:FindFirstChild("AreaEggSlotsClient")
	local v574 = v570 or areaEggSlotsClient and areaEggSlotsClient:FindFirstChild(p26)
	if v574 then
		local ok6, result6 = pcall(function()
			return v574:GetPivot().Position
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
		local pos = v58(p26) or p27.Pos
		p27.Pos = pos
	end
	return p27.Pos
end
local v92 = function(p28, p29)
	local v583 = { At = 0 }
	local v586 = 0
	local v587 = 1
	while not state.Carrying and v586 < p29 and not state.Cancel do
		local rootPart3 = getHumanoidRootPart()
		local pos2 = getPos(p28, v583)
		local v591 = math.max(RunService.Heartbeat:Wait(), 0.004166666666666667)
		v586 += v591
		v587 += v591
		if not rootPart3 or not pos2 then
			if config.GrabInterval <= v587 then
				v587 = 0
				task.spawn(carryFieldEgg, p28)
			end
		else
			local position2 = pos2 - rootPart3.Position
			if 2 < position2.Magnitude then
				local v593 = math.max(getWalkSpeed() * config.SpeedCap, 16)
				local v594 = position2 / math.max(0.08, v591)
				if v593 < v594.Magnitude then
					v594 = v594.Unit * v593
				end
				pcall(function()
					rootPart3.AssemblyLinearVelocity = v594 + Vector3.new(0, workspace.Gravity * v591 * 0.5, 0)
					rootPart3.AssemblyAngularVelocity = Vector3.zero
				end)
			end
			if config.GrabInterval <= v587 then
				v587 = 0
				local v596 = v54(pos2 - Vector3.new(0, 3, 0), 10)
				if v596 and typeof(fireproximityprompt) == "function" then
					pcall(function()
						v596.HoldDuration = 0
					end)
					pcall(fireproximityprompt, v596)
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
local v93 = function(p30)
	if state.Carrying then
		return true
	end
	local rootPart5 = getHumanoidRootPart()
	local v603 = v58(p30)
	if not rootPart5 or not v603 or not (config.RegrabFar < Vector3.new(rootPart5.Position.X - v603.X, 0, rootPart5.Position.Z - v603.Z).Magnitude) then
		v62("The egg fell close, taking it back")
	else
		v62("The egg fell far behind, teleporting onto it")
		angles(v603 + Vector3.new(0, 3, 0))
	end
	return v92(p30, 3)
end
local raycast = function(p31, p32)
	local v605 = p32
	pcall(function()
		local raycastParams = RaycastParams.new()
		raycastParams.FilterType = Enum.RaycastFilterType.Exclude
		raycastParams.FilterDescendantsInstances = { localPlayer.Character }
		raycastParams.IgnoreWater = true
		local raycastResult = workspace:Raycast(p31 + Vector3.new(0, 60, 0), Vector3.new(0, -140, 0), raycastParams)
		if raycastResult and raycastResult.Material ~= Enum.Material.Water and math.abs(raycastResult.Position.Y - p32) < 40 then
			v605 = raycastResult.Position.Y + 3.5
		end
	end)
	return v605
end
local v98 = function(p33)
	local walkSpeed2 = getWalkSpeed()
	local mult = walkSpeed2 * math.min(config.CarryRatio, config.SpeedCap) * state.Mult
	local v613 = mult * 1.5
	local v614 = 5.5 * mult
	local v615 = v613
	if p33 and v614 < p33 then
		v615 = math.min(v613, mult * p33 / (p33 - v614))
	end
	return math.min(math.max(math.min(mult * config.EasyRatio, v615), mult), math.max(walkSpeed2 * config.SpeedCap, mult))
end
local v101 = function(p34, p35)
	local now6 = os.clock()
	local v618 = v79()
	local v619 = p34 - 7
	local height = config.Height
	local v621 = math.clamp(config.ClimbShare, 0.1, 0.9)
	local v622 = height * math.sqrt(1 - v621 * v621) / v621
	local character10 = localPlayer.Character
	local humanoid4 = character10 and character10:FindFirstChildOfClass("Humanoid")
	if humanoid4 then
		humanoid4.PlatformStand = false
	end
	local rootPart6 = getHumanoidRootPart()
	if rootPart6 and character10 and 0.5 < height and rootPart6.Position.Y < v618.Y + height - 2 then
		pcall(function()
			character10:PivotTo(CFrame.new(Vector3.new(rootPart6.Position.X, v618.Y + height, rootPart6.Position.Z)) * rootPart6.CFrame.Rotation)
			rootPart6.AssemblyLinearVelocity = Vector3.zero
			rootPart6.AssemblyAngularVelocity = Vector3.zero
		end)
	end
	local rootPart7 = getHumanoidRootPart()
	local v630 = v98(rootPart7 and Vector3.new(rootPart7.Position.X - v618.X, 0, rootPart7.Position.Z - v618.Z).Magnitude + math.max(0, height) * 2 or nil)
	local now7 = os.clock()
	local v632 = 0
	v62("Safe zone")
	while true do
		if state.Carrying and state.Delivered < now6 and not state.Cancel and v632 < 25 then
			rootPart6 = getHumanoidRootPart()
			if not rootPart6 then
				break
			end
			local now8 = os.clock()
			local v637 = math.max(now8 - now7, 0.004166666666666667)
			now7 = now8
			v632 += v637
			local v638 = rootPart6.Position.X <= v619 + 2
			local v639 = v638 and v618 or Vector3.new(v619, v618.Y, p35)
			if not v638 or not (Vector3.new(v618.X - rootPart6.Position.X, 0, v618.Z - rootPart6.Position.Z).Magnitude < 2) then
				local v640 = v86(rootPart6.Position, v639)
				local vector3 = Vector3.new(v640.X - rootPart6.Position.X, 0, v640.Z - rootPart6.Position.Z)
				local v642 = v638 and 0 or math.max(0, rootPart6.Position.X - v619)
				local v643 = v618.Y + height
				if v638 or v642 <= v622 then
					v643 = v618.Y + height * math.clamp(v642 / math.max(v622, 1), 0, 1)
				end
				local v644 = math.clamp((v643 - rootPart6.Position.Y) / 0.12, -v630 * v621, v630 * v621)
				local v645 = math.sqrt(math.max(v630 * v630 - v644 * v644, 0))
				local zero2 = vector3.Magnitude > 0.01 and vector3.Unit * math.min(v645, vector3.Magnitude / 0.05) or Vector3.zero
				pcall(function()
					rootPart6.AssemblyLinearVelocity = Vector3.new(zero2.X, v644, zero2.Z)
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
local v104 = function(p36)
	local now9 = os.clock()
	local v653, v654 = v76()
	local rootPart8 = getHumanoidRootPart()
	if not rootPart8 then
		return false
	end
	local v656 = math.clamp(rootPart8.Position.Z, -425, -300)
	local vector4 = Vector3.new(v653 + config.LandOffset, v654 + 3.35, v656)
	local v658 = math.max(getWalkSpeed() * config.HopRatio, config.HopMin)
	local hopLift = rootPart8.Position.Y + config.HopLift
	local x = rootPart8.Position.X
	local count2 = 0
	local v662 = v63()
	state.ReleaseCamera = v662
	pcall(v73)
	v67()
	while vector4.X < x - v658 and not state.Cancel do
		if state.Carrying then
			local v664 = x - v658
			v62(string.format("Instant TP: hopping home, X %d", math.floor(v664)))
			local pulledAt = state.PulledAt
			local elapsed3 = 0
			while elapsed3 < config.HopGap do
				angles(Vector3.new(v664, hopLift, v656))
				elapsed3 += RunService.Heartbeat:Wait()
			end
			local rootPart9 = getHumanoidRootPart()
			if not (state.PulledAt > pulledAt or rootPart9 ~= nil and (rootPart9.Position.X - v664 > 10 or rootPart9.AssemblyLinearVelocity.Magnitude > 150)) or not state.Carrying or state.Cancel then
				x = v664
				config.HopGap = math.max(config.HopGapMin, config.HopGap - 0.01)
			else
				count2 += 1
				config.HopGap = math.min(config.HopGapMax, config.HopGap + 0.02)
				if config.HopRetries < count2 then
					break
				end
				v62(string.format("Instant TP: pulled back, retry %d/%d", count2, config.HopRetries))
				local elapsed4 = 0
				while elapsed4 < 0.12 and not state.Cancel do
					angles(Vector3.new(x, hopLift, v656))
					elapsed4 += RunService.Heartbeat:Wait()
				end
			end
		else
			if not v93(p36) then
				break
			end
			local rootPart10 = getHumanoidRootPart()
			if rootPart10 then
				x = math.min(x, rootPart10.Position.X)
			end
		end
	end
	v62("Instant TP: landing next to the line")
	local vector5 = Vector3.new(vector4.X, raycast(vector4, vector4.Y), vector4.Z)
	local v675 = function()
		local rootPart11 = getHumanoidRootPart()
		if not rootPart11 or not (rootPart11.Position.X <= vector5.X + 1) or not (vector5.Y - 25 < rootPart11.Position.Y) then
			angles(vector5)
			return
		end
		pcall(function()
			rootPart11.AssemblyLinearVelocity = Vector3.zero
		end)
	end
	v675()
	for i = 1, 3 do
		local elapsed5 = 0
		while elapsed5 < config.LandSettle and not state.Cancel do
			elapsed5 += RunService.Heartbeat:Wait()
		end
		local rootPart12 = getHumanoidRootPart()
		if not state.Carrying or not rootPart12 or not (12 < rootPart12.Position.X - vector5.X) and not (rootPart12.Position.Y < vector5.Y - 25) then
			break
		end
		v62("Instant TP: landing retry " .. i)
		v675()
	end
	v72()
	if state.Carrying and not state.Cancel then
		local elapsed6 = 0
		while elapsed6 < config.DropDelay do
			elapsed6 += RunService.Heartbeat:Wait()
		end
		v62("Instant TP: dropping the egg")
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
		v662()
		v72()
		v62("Instant TP: taking the egg back")
		if not v92(p36, 3) and not v93(p36) then
			v70()
			v62("Could not take the egg back")
			return false
		end
		v72()
	end
	v662()
	local v683 = false
	if state.Carrying then
		v683 = v101(v653, v656)
	end
	v70()
	return v683 or state.Delivered >= now9
end
local v105 = function(p37)
	if state.Busy then
		return
	end
	state.Busy = true
	state.Cancel = false
	task.spawn(function()
		local ok7, result7 = pcall(function()
			v62("Looking for the egg")
			local v705 = v58(p37)
			if not v705 then
				v62("That egg is gone")
				return
			end
			if state.Carrying and state.Uid ~= p37 then
				dropFieldEgg()
				local elapsed8 = 0
				while state.Carrying and elapsed8 < 1 do
					elapsed8 += RunService.Heartbeat:Wait()
				end
			end
			v67()
			v62("Going to the egg")
			if not v88(p37, v705) then
				v70()
				v62("Could not reach the egg")
				return
			end
			v62("Taking the egg")
			if not v92(p37, 4) then
				v70()
				v62("The egg would not come free")
				return
			end
			v62(v104(p37) and "Delivered" or "Delivery failed")
		end)
		v70()
		v72()
		if state.ReleaseCamera then
			pcall(state.ReleaseCamera)
			state.ReleaseCamera = nil
		end
		if not ok7 then
			v62("Error: " .. tostring(result7))
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
local v107 = {
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
	local v715 = Instance.new(className)
	for key16, value16 in pairs(props or {}) do
		v715[key16] = value16
	end
	return v715
end
local v113 = function(p40, p41)
	local v724 = create
	local v725 = "UICorner"
	local v726 = {}
	v726.CornerRadius = UDim.new(0, p41 or 6)
	local v730 = v724(v725, v726)
	v730.Parent = p40
	return v730
end
local v115 = function(p42, p43, p44, p45)
	local v732 = create
	local v733 = "UIStroke"
	local v734 = { Color = p43 }
	v734.Thickness = p44 or 1
	v734.Transparency = p45 or 0
	v734.ApplyStrokeMode = Enum.ApplyStrokeMode.Border
	local v737 = v732(v733, v734)
	v737.Parent = p42
	return v737
end
local tween = function(p46, p47, p48, p49, p50)
	local v741 = TweenService
	v741:Create(p46, TweenInfo.new(p47 or 0.2, p49 or Enum.EasingStyle.Quint, p50 or Enum.EasingDirection.Out), p48):Play()
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
v113(tokinuEggFrame, 18)
v115(tokinuEggFrame, colors.AQUA_STROKE, 1.2, 0.3)
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
	local v750 = nil
	local v751 = nil
	local position3 = nil
	local position4 = nil
	local v754 = function(p53)
		local v756 = p53 - position3
		p51.Position = UDim2.new(position4.X.Scale, position4.X.Offset + v756.X, position4.Y.Scale, position4.Y.Offset + v756.Y)
	end
	p52.InputBegan:Connect(function(input)
		if input.UserInputType == Enum.UserInputType.MouseButton1 or input.UserInputType == Enum.UserInputType.Touch then
			v750 = true
			v751 = input
			position3 = input.Position
			position4 = p51.Position
			input.Changed:Connect(function()
				if input.UserInputState == Enum.UserInputState.End then
					v750 = false
				end
			end)
		end
	end)
	UserInputService.InputChanged:Connect(function(input2)
		if v750 and (input2.UserInputType == Enum.UserInputType.MouseMovement or input2.UserInputType == Enum.UserInputType.Touch) then
			v754(input2.Position)
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
v113(foldButton, 7)
local v127 = v115(foldButton, colors.AQUA_STROKE, 1, 0.5)
foldButton.MouseEnter:Connect(function()
	tween(foldButton, 0.14, { BackgroundColor3 = Color3.fromRGB(40, 65, 115), BackgroundTransparency = 0.05 })
	tween(v127, 0.14, { Transparency = 0.3 })
end)
foldButton.MouseLeave:Connect(function()
	tween(foldButton, 0.14, { BackgroundColor3 = colors.OFF_BG, BackgroundTransparency = 0.1 })
	tween(v127, 0.14, { Transparency = 0.5 })
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
v113(contentBoxFrame, 16)
v115(contentBoxFrame, colors.AQUA_STROKE, 1, 0.4)
local innerFrame = create("Frame", {
	Name = "Inner",
	Size = UDim2.new(1, -10, 1, -10),
	Position = UDim2.fromOffset(5, 5),
	BackgroundTransparency = 1,
	ZIndex = 102,
})
innerFrame.Parent = contentBoxFrame
local v131 = false
local size = nil
foldButton.MouseButton1Click:Connect(function()
	v131 = not v131
	if v131 then
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
v113(stealButton, 11)
local v136 = v115(stealButton, colors.AQUA_STROKE, 1, 0.5)
stealButton.MouseEnter:Connect(function()
	tween(stealButton, 0.14, { BackgroundColor3 = Color3.fromRGB(40, 65, 115), BackgroundTransparency = 0.05 })
	tween(v136, 0.14, { Transparency = 0.3 })
end)
stealButton.MouseLeave:Connect(function()
	tween(stealButton, 0.14, { BackgroundColor3 = colors.OFF_BG, BackgroundTransparency = 0.1 })
	tween(v136, 0.14, { Transparency = 0.5 })
end)
local v139 = {
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
v62 = function(p56)
	local text3 = tostring(p56)
	if v139[text3] or string.sub(text3, 1, 9) == "Selected:" then
		statusLabel.Text = text3
	elseif string.sub(text3, 1, 5) == "Error" then
		statusLabel.Text = "Something went wrong"
	elseif state.Busy then
		statusLabel.Text = "Delivering"
	end
end
local v140 = {}
local v141 = nil
local v143 = function(p57)
	v141 = p57
	for key17, value17 in pairs(v140) do
		local v778 = key17 == p57
		value17.Stroke.Thickness = v778 and 1.5 or 1
		value17.Stroke.Color = v778 and colors.AQUA_STROKE or colors.OFF_BG
		value17.Stroke.Transparency = v778 and 0.2 or 0.45
	end
end
local v144 = function(p58)
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
	v113(eggRowButton, 11)
	local v787 = v115(eggRowButton, colors.OFF_BG, 1, 0.45)
	local imageLabel = create("ImageLabel", {
		Size = UDim2.fromOffset(22, 22),
		Position = UDim2.fromOffset(3, 3),
		BackgroundTransparency = 1,
		Image = p58.Icon,
		ScaleType = Enum.ScaleType.Fit,
		ZIndex = 104,
	})
	imageLabel.Parent = eggRowButton
	local v790 = create
	local v791 = "TextLabel"
	local v792 = {
		Size = UDim2.new(1, -30, 0, 13),
		Position = UDim2.fromOffset(28, 2),
		BackgroundTransparency = 1,
		Font = Enum.Font.GothamBold,
		TextSize = 10,
		TextXAlignment = Enum.TextXAlignment.Left,
		TextTruncate = Enum.TextTruncate.AtEnd,
		Text = p58.Name,
	}
	local text4 = v107[math.clamp(p58.Rarity, 1, #v107)] or colors.TEXT
	v792.TextColor3 = text4
	v792.ZIndex = 104
	local v794 = v790(v791, v792)
	v794.Parent = eggRowButton
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
		v143(p58.Uid)
		v62("Selected: " .. p58.Name)
	end)
	eggRowButton.MouseEnter:Connect(function()
		tween(eggRowButton, 0.14, { BackgroundColor3 = Color3.fromRGB(40, 65, 115), BackgroundTransparency = 0.05 })
		tween(v787, 0.14, { Transparency = 0.3 })
	end)
	eggRowButton.MouseLeave:Connect(function()
		tween(eggRowButton, 0.14, { BackgroundColor3 = colors.SURF2, BackgroundTransparency = colors.SURF2_TRANS })
		tween(v787, 0.14, { Transparency = 0.45 })
	end)
	return { Frame = eggRowButton, Stroke = v787, Icon = imageLabel, Name = v794, Value = textLabel, Info = p58 }
end
local v147 = function(p59, p60)
	p59.Info = p60
	if p59.Icon.Image ~= p60.Icon then
		p59.Icon.Image = p60.Icon
	end
	if p59.Name.Text ~= p60.Name then
		p59.Name.Text = p60.Name
	end
	local text5 = v107[math.clamp(p60.Rarity, 1, #v107)] or colors.TEXT
	if p59.Name.TextColor3 ~= text5 then
		p59.Name.TextColor3 = text5
	end
	local v804 = formatText(p60.Value)
	if p59.Value.Text ~= v804 then
		p59.Value.Text = v804
	end
end
local v148 = function()
	local list3 = {}
	for _, item6 in ipairs(askFieldEggSnapshot()) do
		list3[#list3 + 1] = v59(item6)
	end
	table.sort(list3, function(a, b)
		return a.Value > b.Value
	end)
	local seen2 = {}
	for index, item7 in ipairs(list3) do
		seen2[item7.Uid] = true
		local v828 = v140[item7.Uid]
		if v828 then
			v147(v828, item7)
			if v828.Frame.LayoutOrder ~= index then
				v828.Frame.LayoutOrder = index
			end
		else
			local v830 = v144(item7)
			v830.Frame.LayoutOrder = index
			v140[item7.Uid] = v830
		end
	end
	for key20, value20 in pairs(v140) do
		if not seen2[key20] then
			value20.Frame:Destroy()
			v140[key20] = nil
		end
	end
	if #list3 == 0 then
		v62("Pick a pet")
	end
end
task.spawn(function()
	while tokinuEggGui.Parent do
		task.wait(4)
		if not state.Busy then
			pcall(v148)
		end
	end
end)
stealButton.MouseButton1Click:Connect(function()
	if state.Busy then
		state.Cancel = true
		v62("Cancelling")
		return
	end
	if not v141 then
		v62("Pick a pet")
		return
	end
	v105(v141)
end)
task.spawn(function()
	while tokinuEggGui.Parent do
		task.wait(0.25)
		stealButton.Text = state.Busy and "CANCEL" or "STEAL"
	end
end)
v148()
v43()
local v151 = {
	{
	find = "You stole an EGG",
	replace = "<font color=\"#ffffff\"><stroke color=\"#000000\" thickness=\"2\" joins=\"round\">You stole an <font color=\"#23272e\">EGG</font> using <font color=\"#4287f5\">Tokinu Hub</font></stroke></font>",
},
}
local v152 = { lt = "<", gt = ">", amp = "&", quot = "\"", apos = "'" }
local seen3 = {}
local v156 = function(p63)
	local v836 = p63:gsub("<[^>]->", ""):gsub("&(%a+);", v152):gsub("%s+", " "):match("^%s*(.-)%s*$")
	return v836
end
local v159 = function(p64, p65, p66)
	local v838, v839 = p64:find(p65, 1, true)
	if v838 then
		return p64:sub(1, v838 - 1) .. p66 .. p64:sub(v839 + 1)
	end
	return p66
end
local v160 = function(p67)
	pcall(function()
		local text = p67.Text
		local v848 = v156(text)
		if v848:lower() == "invalid code" then
			p67.Text = v159(text, v848, "this code is invalid boi")
			return
		end
		for _, item8 in ipairs(v151) do
			if v848 == item8.find then
				if item8.replace:find("<[^>]+>") and not p67.RichText then
					p67.RichText = true
				end
				p67.Text = item8.replace
				return
			end
		end
	end)
end
local v162 = function(instance2)
	local v858 = false
	pcall(function()
		v858 = instance2:IsA("TextLabel") or instance2:IsA("TextButton") or instance2:IsA("TextBox")
	end)
	if not v858 or seen3[instance2] then
		return
	end
	seen3[instance2] = true
	v160(instance2)
	instance2:GetPropertyChangedSignal("Text"):Connect(function()
		v160(instance2)
	end)
end
for _, descendant3 in ipairs(game:GetDescendants()) do
	v162(descendant3)
end
game.DescendantAdded:Connect(v162)
