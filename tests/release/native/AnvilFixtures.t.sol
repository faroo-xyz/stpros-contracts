// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {ReleaseClosureTest, LocalAtomicBatch} from "../ReleaseClosure.t.sol";
import {OracleV3MigrationTest} from "../../protocol/OracleV3Migration.t.sol";
import {ReleaseAssertions} from "../../../contracts/release/ReleaseAssertions.sol";
import {Oracle} from "../../../contracts/Oracle.sol";
import {StPROS} from "../../../contracts/StPROS.sol";
import {BridgeVault} from "../../../contracts/BridgeVault.sol";
import {SlpDistributor} from "../../../contracts/SlpDistributor.sol";
import {YieldVault} from "../../../contracts/YieldVault.sol";
import {YieldVaultFactory} from "../../../contracts/YieldVaultFactory.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";
import {ILegacyProxyAdminV4, ILegacyYieldVaultFactoryV3, ILegacyYieldVaultV3, LegacyYieldVaultV3Types} from "../../helpers/LegacyInterfaces.sol";
import {Vm} from "forge-std/Vm.sol";

library CanonicalRuntime {
    Vm internal constant VM=Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    function install(address target,string memory name,address immutableAddress) internal {
        string memory input=VM.readFile(string.concat("cache/native-r4/",name,"/artifact.json"));
        bytes memory code=VM.parseJsonBytes(input,".deployedBytecode");
        string[] memory ids=VM.parseJsonKeys(input,".immutableReferences");
        if(immutableAddress==address(0))require(ids.length==0,"unexpected immutable");
        else {
            require(ids.length==1,"factory immutable coverage");
            string memory base=string.concat(".immutableReferences.",ids[0]);
            // The canonical artifact has a static pair of uint256 fields per range.
            uint256[2][] memory ranges=abi.decode(VM.parseJson(input,base),(uint256[2][]));
            bytes32 word=bytes32(uint256(uint160(immutableAddress)));
            for(uint256 i;i<ranges.length;++i){
                // JSON object keys are ABI encoded alphabetically: length, start.
                uint256 length=ranges[i][0];uint256 start=ranges[i][1];
                require(length==32&&start+32<=code.length,"immutable range");
                for(uint256 k;k<32;++k){require(code[start+k]==0,"immutable template");code[start+k]=word[k];}
            }
        }
        VM.etch(target,code);
    }
}

/// Test-only state materialization. No key, signature, public RPC or broadcast.
/// The Python rehearsal loads this local state and executes actual contract calls.
contract AnvilMainnetFixture is ReleaseClosureTest {
    function _export(bool broken) internal {
        // dumpState exports the current test journal; materialize setup here too.
        setUp();
        _beforeH();
        CanonicalRuntime.install(oi,"Oracle",address(0));
        CanonicalRuntime.install(si,"StPROS",address(0));
        CanonicalRuntime.install(vi,"YieldVault",address(0));
        CanonicalRuntime.install(fi,"YieldVaultFactory",vi);
        CanonicalRuntime.install(address(tl),"TimelockController",address(0));
        CanonicalRuntime.install(address(checks),"ReleaseAssertions",address(0));
        CanonicalRuntime.install(ILegacyProxyAdminV4(legacy.proxyAdmin).getProxyImplementation(address(bridge)),"BridgeVault",address(0));
        CanonicalRuntime.install(ILegacyProxyAdminV4(legacy.proxyAdmin).getProxyImplementation(address(sink)),"SlpDistributor",address(0));
        (address[] memory u,uint256[] memory values,bytes[] memory data)=_u(broken);
        bytes32 id=tl.hashOperationBatch(u,values,data,0,0);
        (address[] memory h,bytes[] memory hd)=_h(u,values,data,_common(false),id);
        string memory name=broken?"mainnet-rollback":"mainnet";
        vm.serializeUint(name,"chainId",1672);
        vm.serializeUint(name,"timestamp",block.timestamp);
        vm.serializeUint(name,"delay",600);
        vm.serializeBool(name,"expectRevert",broken);
        vm.serializeAddress(name,"gov",gov);
        vm.serializeAddress(name,"can",can);
        vm.serializeAddress(name,"timelock",address(tl));
        vm.serializeAddress(name,"oracle",address(oracle));
        vm.serializeAddress(name,"stpros",address(st));
        vm.serializeAddress(name,"assertions",address(checks));
        vm.serializeBytes(name,"operationId",abi.encodePacked(id));
        vm.serializeBytes(name,"handoff",abi.encodeCall(LocalAtomicBatch.run,(h,hd)));
        vm.serializeBytes(name,"execute",abi.encodeCall(tl.executeBatch,(u,values,data,0,0)));
        string memory json=vm.serializeBytes(name,"postcheck",abi.encodeCall(ReleaseAssertions.assertR4,(_common(true),_core())));
        vm.createDir("cache_forge/rehearsal",true);
        vm.writeJson(json,string.concat("cache_forge/rehearsal/",name,".json"));
        vm.dumpState(string.concat("cache_forge/rehearsal/",name,"-state.json"));
    }
    function test_exportAnvilMainnet() public {_export(false);}
    function test_exportAnvilRollback() public {_export(true);}
}

/// Full 15-vault local recorded-V3 topology with E1/E2/GOV kept distinct.
contract AnvilTestnetFixture is OracleV3MigrationTest {
    ILegacyYieldVaultFactoryV3 internal factory;
    address[] internal vaults;
    address internal e2=makeAddr("testnet-E2");
    address internal newGov=makeAddr("testnet-GOV");
    address internal can=makeAddr("testnet-CAN");
    address internal ops=makeAddr("testnet-OPS");
    ReleaseAssertions internal checks;
    address[5] internal proxies;
    address[5] internal implementations;
    address internal vi;
    bool internal unavailable;
    bool internal incomplete;

    function _prepare() internal {
        vm.chainId(688689);
        address[] memory actors=new address[](1);actors[0]=newGov;
        tl=new TimelockController(300,actors,actors,address(0));
        vm.startPrank(address(tl));tl.grantRole(tl.CANCELLER_ROLE(),can);vm.stopPrank();
        checks=new ReleaseAssertions();
        factory=ILegacyYieldVaultFactoryV3(_deployTransparentProxyV4("testnet",_deployRecorded("testnet","YieldVaultFactory_V3"),proxyAdmin,abi.encodeCall(ILegacyYieldVaultFactoryV3.initialize,(e2))));
        address vaultImpl=_deployRecorded("testnet","YieldVault_V3");
        address counterparty=makeAddr("testnet-counterparty");
        vm.startPrank(e2);
        factory.upgradeBeaconTo(vaultImpl);
        factory.addCounterpartyToWhitelist(counterparty);
        LegacyYieldVaultV3Types.RoundParams memory round=LegacyYieldVaultV3Types.RoundParams(1 days,7 days,600,100 ether,0,1 ether,0,5000);
        for(uint256 i;i<15;++i){
            address v=factory.createYieldVault(LegacyYieldVaultV3Types.InitParams(address(st),address(factory),e2,counterparty,e2,"Local vault","LV",round));
            factory.emergencyCancel(v);vaults.push(v);
        }
        vm.stopPrank();
        vi=address(new YieldVault());
        proxies=[address(old),address(st),address(bridge),address(sink),address(factory)];
        implementations=[implementation,address(new StPROS()),address(new BridgeVault()),address(new SlpDistributor()),address(new YieldVaultFactory(vi))];
        string[5] memory names=[string("Oracle"),"StPROS","BridgeVault","SlpDistributor","YieldVaultFactory"];
        for(uint256 i;i<5;++i)CanonicalRuntime.install(implementations[i],names[i],i==4?vi:address(0));
        CanonicalRuntime.install(vi,"YieldVault",address(0));
        CanonicalRuntime.install(address(tl),"TimelockController",address(0));
        CanonicalRuntime.install(address(checks),"ReleaseAssertions",address(0));
    }

    function _expected(bool post) internal view returns(ReleaseAssertions.Common memory e){
        e.chainId=688689;e.timelock=address(tl);e.timelockHash=address(tl).codehash;e.minDelay=300;
        e.gov=newGov;e.can=can;e.guardian=guardian;e.ops=ops;e.deployer=address(this);
        e.proxyAdmin=proxyAdmin;e.proxyAdminHash=proxyAdmin.codehash;
        e.proxies=new ReleaseAssertions.ProxyCheck[](5);
        for(uint256 i;i<5;++i){
            address impl=post?implementations[i]:ILegacyProxyAdminV4(proxyAdmin).getProxyImplementation(proxies[i]);
            address owner=post?address(tl):(i==4?(unavailable?e2:address(tl)):(incomplete?gov:address(tl)));
            e.proxies[i]=ReleaseAssertions.ProxyCheck(proxies[i],owner,impl,impl.codehash,post,false);
        }
        e.beacons=new ReleaseAssertions.BeaconCheck[](1);
        address current=post?vi:factory.currentImplementation();
        e.beacons[0]=ReleaseAssertions.BeaconCheck(address(factory),factory.beacon(),current,current.codehash,post,false);
        e.vaults=new ReleaseAssertions.VaultCheck[](15);
        for(uint256 i;i<15;++i)e.vaults[i]=ReleaseAssertions.VaultCheck(vaults[i],address(factory),1,checks.roundTermsHash(vaults[i]),3,post,post&&(unavailable||i!=14),!unavailable&&i==14,unavailable);
        e.roles=new ReleaseAssertions.RoleCheck[](20);
        e.roles[0]=ReleaseAssertions.RoleCheck(address(tl),bytes32(0),address(tl),true);
        e.roles[1]=ReleaseAssertions.RoleCheck(address(tl),tl.PROPOSER_ROLE(),newGov,true);
        e.roles[2]=ReleaseAssertions.RoleCheck(address(tl),tl.EXECUTOR_ROLE(),newGov,true);
        e.roles[3]=ReleaseAssertions.RoleCheck(address(tl),tl.CANCELLER_ROLE(),newGov,true);
        e.roles[4]=ReleaseAssertions.RoleCheck(address(tl),tl.CANCELLER_ROLE(),can,true);
        for(uint256 i;i<15;++i)e.roles[5+i]=ReleaseAssertions.RoleCheck(vaults[i],bytes32(0),e2,unavailable);
        e.inventoryHash=keccak256(abi.encode(e.roles));
    }

    function _coreExpected() internal view returns(ReleaseAssertions.Core memory c){
        c.oracle=address(old);c.stpros=address(st);c.bridge=address(bridge);c.distributor=address(sink);c.asset=wpros;
        c.reporter=reporter;c.treasury=treasury;c.keeper=reporter;c.commissionPpm=feePpm;c.aprPpm=TEST_APR;
        c.maxUpdateAmount=absoluteCap;c.interval=3600;c.unbondingPeriod=0;c.maxWithdrawCount=st.maxWithdrawCount();c.destinations=new address[](0);
    }

    function _upgrade() internal view returns(address[] memory t,uint256[] memory v,bytes[] memory d){
        uint256 count=22;
        t=new address[](count);v=new uint256[](count);d=new bytes[](count);
        bytes[5] memory migration=[_data(wpros,TEST_APR),abi.encodeCall(StPROS.migrateFromV2,(guardian,address(tl))),abi.encodeCall(BridgeVault.migrateToR4,(guardian,address(tl))),abi.encodeCall(SlpDistributor.migrateToR4,(guardian,address(tl))),abi.encodeCall(YieldVaultFactory.migrateToR4,(guardian,address(tl)))];
        for(uint256 i;i<5;++i){t[i]=proxyAdmin;d[i]=abi.encodeCall(ILegacyProxyAdminV4.upgradeAndCall,(proxies[i],implementations[i],migration[i]));}
        t[5]=address(factory);d[5]=abi.encodeCall(YieldVaultFactory.upgradeBeaconTo,(vi));
        for(uint256 i;i<15;++i){
            if(i==14&&!unavailable){t[6+i]=vaults[i];d[6+i]=abi.encodeWithSignature("grantRole(bytes32,address)",keccak256("OPERATOR_ROLE"),ops);}
            else{t[6+i]=address(factory);d[6+i]=abi.encodeCall(YieldVaultFactory.retireVault,(vaults[i]));}
        }
        t[count-1]=address(checks);d[count-1]=abi.encodeCall(ReleaseAssertions.assertR4,(_expected(true),_coreExpected()));
    }

    function _export(bool noE2,bool noT3,string memory name) internal {
        setUp();
        unavailable=noE2;incomplete=noT3;_prepare();
        // Materialize the approved actor-specific handoff before local U_t.
        if(!unavailable){
            vm.startPrank(e2);
            for(uint256 i;i<15;++i){if(i==14)ILegacyYieldVaultV3(vaults[i]).grantRole(0,address(tl));ILegacyYieldVaultV3(vaults[i]).renounceRole(0,e2);}
            factory.transferOwnership(address(tl));vm.stopPrank();
        }
        vm.startPrank(gov);
        if(!incomplete){old.transferOwnership(address(tl));st.transferOwnership(address(tl));bridge.transferOwnership(address(tl));sink.transferOwnership(address(tl));}
        ILegacyProxyAdminV4(proxyAdmin).transferOwnership(address(tl));vm.stopPrank();
        (address[] memory t,uint256[] memory v,bytes[] memory d)=_upgrade();
        bytes32 id=tl.hashOperationBatch(t,v,d,0,0);
        vm.serializeUint(name,"chainId",688689);vm.serializeUint(name,"timestamp",block.timestamp);vm.serializeUint(name,"delay",300);
        vm.serializeBool(name,"expectRevert",false);vm.serializeAddress(name,"gov",newGov);vm.serializeAddress(name,"can",can);
        vm.serializeAddress(name,"timelock",address(tl));vm.serializeAddress(name,"oracle",address(old));vm.serializeAddress(name,"stpros",address(st));vm.serializeAddress(name,"assertions",address(checks));
        vm.serializeBytes(name,"operationId",abi.encodePacked(id));
        vm.serializeBytes(name,"handoff",abi.encodeCall(tl.scheduleBatch,(t,v,d,0,0,300)));
        vm.serializeBytes(name,"handoffCheck",noE2||noT3?abi.encodeCall(ReleaseAssertions.assertTestnetRecoveryHandoff,(_expected(false),id)):abi.encodeCall(ReleaseAssertions.assertHandoff,(_expected(false),id)));
        vm.serializeBytes(name,"execute",abi.encodeCall(tl.executeBatch,(t,v,d,0,0)));
        string memory json=vm.serializeBytes(name,"postcheck",abi.encodeCall(ReleaseAssertions.assertR4,(_expected(true),_coreExpected())));
        vm.createDir("cache_forge/rehearsal",true);
        vm.writeJson(json,string.concat("cache_forge/rehearsal/",name,".json"));vm.dumpState(string.concat("cache_forge/rehearsal/",name,"-state.json"));
    }
    function test_exportAnvilTestnet() public {_export(false,false,"testnet");}
    function test_exportAnvilE2Recovery() public {_export(true,false,"testnet-e2");}
    function test_exportAnvilT3Recovery() public {_export(false,true,"testnet-t3");}
    function test_exportAnvilCombinedRecovery() public {_export(true,true,"testnet-combined");}
}
