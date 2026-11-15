local StarterPlayer = game:GetService("StarterPlayer")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local ServerScriptService = game:GetService("ServerScriptService")
local packages = ReplicatedStorage:WaitForChild("Packages")
local Net = require(packages.Net)
local Debounce = require(packages.Debounce)
local ServerAuthority = require(ReplicatedStorage.Shared.ServerAuthority)
local parent = script.Parent
local handle = parent:WaitForChild("Handle")
local beam = handle:WaitForChild("Beam")
local ropeAttachment = handle:WaitForChild("RopeAttachment")
local parent2 = parent.Parent.Parent
local hit = script.Hit
local fire = script.Fire
local v15 = function(p1)
	if not p1.Parent:FindFirstChild("Humanoid") then
		if not p1.Parent.Parent:FindFirstChild("Humanoid") then
			return false
		end
		return true
	end
	return true
end
parent.Activated:Connect(function()
	if not parent2:GetAttribute("Stealing") then
		local PlayerMouse = require(ReplicatedStorage.Packages.PlayerMouse)
		local hit2 = PlayerMouse.Hit
		local target = PlayerMouse.Target
		if not target or v15(target) then
			return
		end
		local position = hit2.Position
		local character = parent2.Character
		if character then
			local tool = character:FindFirstChildWhichIsA("Tool", true)
			if tool then
				if tool.Name == "Grapple Hook" then
					local humanoidRootPart = character:FindFirstChild("HumanoidRootPart")
					if humanoidRootPart then
						local v23 = (position - humanoidRootPart.Position).Unit
						local v24 = (position - humanoidRootPart.Position).Magnitude
						if 10 <= v24 and v24 <= 100 then
							if Debounce(("ItemUse/GrappleHook/Client/%*"):format(parent2.Name), 3) then
								return
							end
							fire:Play()
							Net:RemoteEvent("75c9466d-e4c0-4b02-b26a-c3615fcc1e42"):FireServer(v24 / 120, position)
							local part = Instance.new("Part")
							part.Anchored = true
							part.CanCollide = false
							part.Transparency = 1
							part.Position = position
							part.Size = Vector3.new(0.1, 0.1, 0.1)
							part.Parent = workspace
							local attachment = Instance.new("Attachment")
							attachment.Position = Vector3.zero
							attachment.Parent = part
							beam.Attachment0 = attachment
							local v27 = nil
							if not ServerAuthority.isEnabled() then
								local bodyVelocity = Instance.new("BodyVelocity")
								bodyVelocity.Name = "FlightPower"
								bodyVelocity.MaxForce = Vector3.new(math.huge, math.huge, math.huge)
								bodyVelocity.Velocity = v23 * 100
								bodyVelocity.P = 2000
								bodyVelocity.Parent = humanoidRootPart
								v27 = bodyVelocity
							end
							hit:Play()
							local v29 = nil
							local v30 = nil
							local v31 = function()
								if v29 then
									v29:Disconnect()
									v29 = nil
								end
								if v30 then
									if coroutine.status(v30) == "suspended" then
										pcall(task.cancel, v30)
									end
									v30 = nil
								end
								if v27 then
									v27:Destroy()
								end
								attachment:Destroy()
								part:Destroy()
								beam.Attachment0 = nil
							end
							v29 = parent2:GetAttributeChangedSignal("Stealing"):Connect(function()
								if parent2:GetAttribute("Stealing") then
									task.defer(v31)
								end
							end)
							v30 = task.delay(v24 / 120, function()
								task.defer(v31)
							end)
							if parent2:GetAttribute("Stealing") then
								task.defer(v31)
							end
						end
					end
				end
			end
		end
	end
end)
