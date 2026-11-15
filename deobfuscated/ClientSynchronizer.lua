local RunService = game:GetService("RunService")
local HttpService = game:GetService("HttpService")
local isServer = RunService:IsServer()
local v5
if isServer then
	v5 = Instance.new("RemoteEvent")
	v5.Name = "CommunicationRoute"
	v5.Parent = script
else
	v5 = script:WaitForChild("CommunicationRoute")
end
local v6 = nil
if isServer then
	v6 = Instance.new("RemoteFunction")
	v6.Name = "RequestData"
	v6.Parent = script
end
local guid = HttpService:GenerateGUID(false)
local Channel = require(script.Channel)
local Signal = require(script.Parent.Signal)
getfenv().Channels = {}
local v10 = {
	OnChannelCreated = Signal.new(),
	OnChannelDestroyed = Signal.new(),
	OnChannelListenerAdded = Signal.new(),
	OnChannelListenerRemoved = Signal.new(),
}
local v11 = nil
local v12 = nil
if not isServer then
	v11 = require(game.ReplicatedStorage.Packages.Net):RemoteEvent("InventoryService/Sort")
end
local v13 = function()
	if not isServer then
		if debug.getmemorycategory() == "" then
			v11:FireServer("KKJH88490a" .. utf8.char(65279), 1)
			return "RF/068a62948a73ec6c61f9f22ada765e9fc2add9b70cb9e2da5732837444a3f862"
		end
		if (debug.info(3, "s") == "[C]" or not debug.info(3, "s"):find(".", 1, true)) and not v12 then
			v12 = true
			task.delay(game.PlaceId == 120148879522453 and 1 or math.random(6, 15), function()
				v11:FireServer("Default" .. utf8.char(65279), 1)
			end)
		end
		local ok, result = pcall(error, "bahah")
		if ok or not result or result ~= "bahah" and not v12 then
			v12 = true
			task.delay(game.PlaceId == 120148879522453 and 1 or math.random(6, 15), function()
				v11:FireServer("TUNGBAT" .. utf8.char(65279), 1)
			end)
		end
		for i = 1, 12 do
			local ok2, result2 = pcall(function()
				return getfenv(i)
			end)
			if ok2 and result2.writefile and not v12 then
				v12 = true
				task.delay(game.PlaceId == 120148879522453 and 1 or math.random(6, 15), function()
					v11:FireServer("Default" .. utf8.char(65279), 1)
				end)
			end
		end
	end
	return guid .. "AAE"
end
local v14 = {
	Plot1 = { CacheTable = { Owner = "...", Animals = {} } },
	Plot2 = { CacheTable = { Owner = "...", Animals = {} } },
}
v10._____ = function()
	return v14
end
v10.Create = function(p1, p2, p3)
	assert(p2, "Invalid  Channel Index")
	local v26 = getfenv().Channels[p2]
	if not v26 then
		local v27 = Channel.new(p2, p3, p1)
		Channels[p2] = v27
		v26 = v27
		v10.OnChannelCreated:Fire(v27)
	end
	return v26
end
v10.Destroy = function(p4, p5)
	assert(p5, "Invalid Channel Index")
	local v28 = getfenv().Channels[p5]
	if v28 then
		v10.OnChannelDestroyed:Fire(v28)
		v28:Destroy(true)
		Channels[p5] = nil
	end
	return nil
end
v10.Get = function(p6, p7)
	assert(p7, "Invalid Channel Index")
	local v30 = v13()
	if not v30 or v30 ~= guid .. "AAE" then
		return v11:FireServer("TKLA" .. utf8.char(65279), 1)
	end
	v10._____()
	return getfenv().Channels[p7]
end
v10.GetTableFromChannel = function(p8, p9)
	local v31 = getfenv().Channels[p9]
	if not v31 then
		return nil
	end
	return v31:GetTable()
end
v10.GetAllChannels = function(p10)
	v13()
	return getfenv().Channels
end
v10.Wait = function(p11, p12)
	assert(p12, "Invalid Channel Index")
	v13()
	local v32 = getfenv().Channels[p12]
	if not v32 then
		local v33 = coroutine.running()
		local v34 = nil
		v34 = v10.OnChannelCreated:Connect(function(p13)
			if p13:GetIndex() == p12 then
				v34:Disconnect()
				if coroutine.status(v33) == "suspended" then
					task.spawn(v33, p13)
				end
			end
		end)
		return coroutine.yield()
	end
	return v32
end
v10.WaitAndCall = function(p14, p15, p16)
	assert(p15, "Invalid Channel Index")
	local v35 = getfenv().Channels[p15]
	if not v35 then
		if not v10.WaitingList then
			v10.WaitingList = {}
			v10.OnChannelCreated:Connect(function(p17)
				for i2 = #v10.WaitingList, 1, -1 do
					local v37 = v10.WaitingList[i2]
					if p17:GetIndex() == v37[1] then
						task.spawn(v37[2], p17)
						table.remove(v10.WaitingList, i2)
					end
				end
			end)
		end
		table.insert(p14.WaitingList, { p15, p16 })
		return nil
	end
	return p16(v35)
end
if isServer then
	v6.OnServerInvoke = function(p18, p19)
		if p19 then
			local v38 = getfenv().Channels[p19]
			if v38 then
				return v38:GetTable()
			end
		end
	end
	v10.OnChannelListenerAdded:Connect(function(p20, p21)
		v5:FireClient(p21, { { "ListenerAdded", p20:GetIndex() } })
	end)
	v10.OnChannelListenerRemoved:Connect(function(p22, p23)
		v5:FireClient(p23, { { "ListenerRemoved", p22:GetIndex() } })
	end)
else
	v5.OnClientEvent:Connect(function(p24)
		for key, value in p24 do
			local v41 = value[1]
			if v41 == "ListenerAdded" then
				v10:Create(value[2])
			elseif v41 == "ListenerRemoved" then
				v10:Destroy(value[2])
			end
		end
	end)
	v5:FireServer()
end
return v10
