// SPDX-License-Identifier: GPL-3.0
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {ReleaseAssertions} from "../../contracts/release/ReleaseAssertions.sol";
import {YieldVault} from "../../contracts/YieldVault.sol";
import {YieldVaultFactory} from "../../contracts/YieldVaultFactory.sol";
import {TimelockController} from "@openzeppelin/contracts/governance/TimelockController.sol";

/// Independent Solidity compiler/OZ oracle. Test VM only; no RPC or signing.
contract FoundationWitnessTest is Test {
    address constant A = 0x1212121212121212121212121212121212121212;
    bytes32 constant H = 0xabababababababababababababababababababababababababababababababab;
    struct Nested { uint256 a; bytes[] b; }
    struct HexFields { bytes3 tag; bytes[][] payloads; uint256 amount; address target; string memo; }
    function ref(string memory name) internal view returns(bytes memory) {
        return vm.parseJsonBytes(vm.readFile("test/fixtures/native-foundation/witnesses.json"),string.concat(".",name));
    }
    function test_keccakRateBoundaryVectors() public view {
        uint256[12] memory lengths=[uint256(0),1,3,32,135,136,137,200,271,272,273,1024];
        for(uint256 n;n<lengths.length;n++) {
            bytes memory b=new bytes(lengths[n]);for(uint256 i;i<b.length;i++)b[i]=bytes1(uint8(i));
            assertEq(abi.encodePacked(keccak256(b)),ref(string.concat("hash_",vm.toString(lengths[n]))));
        }
        assertEq(abi.encodePacked(keccak256("PROPOSER_ROLE")),ref("hash_text_0"));
        assertEq(abi.encodePacked(keccak256("transfer(address,uint256)")),ref("hash_text_3"));
    }
    function test_nestedDynamicAndFixedArrays() public view {
        Nested[] memory rows=new Nested[](1);rows[0].a=7;rows[0].b=new bytes[](2);rows[0].b[0]="";rows[0].b[1]=hex"1234";
        assertEq(abi.encode(rows),ref("abi_nested"));
        uint256[2][] memory matrix=new uint256[2][](2);matrix[0]=[uint256(1),2];matrix[1]=[uint256(3),4];
        assertEq(abi.encode(matrix),ref("abi_matrix"));
        string[2] memory names=[string("x"),unicode"汉"];assertEq(abi.encode(names),ref("abi_fixed_dynamic"));
        bytes32[12] memory terms;for(uint256 i;i<12;i++)terms[i]=bytes32(i);assertEq(abi.encode(terms),ref("abi_terms"));
        Nested[] memory decoded=abi.decode(ref("abi_nested"),(Nested[]));assertEq(decoded[0].a,7);assertEq(decoded[0].b[1],hex"1234");
    }
    function test_realAssertionTuplesAndSelectors() public view {
        ReleaseAssertions.Common memory c;
        c.chainId=1;c.timelock=A;c.timelockHash=H;c.minDelay=1;c.gov=A;c.can=A;c.guardian=A;c.ops=A;c.deployer=A;c.proxyAdmin=A;c.proxyAdminHash=H;c.inventoryHash=H;
        c.roles=new ReleaseAssertions.RoleCheck[](1);c.roles[0]=ReleaseAssertions.RoleCheck(A,H,A,true);
        c.proxies=new ReleaseAssertions.ProxyCheck[](1);c.proxies[0]=ReleaseAssertions.ProxyCheck(A,A,A,H,true,true);
        c.beacons=new ReleaseAssertions.BeaconCheck[](1);c.beacons[0]=ReleaseAssertions.BeaconCheck(A,A,A,H,true,true);
        c.vaults=new ReleaseAssertions.VaultCheck[](1);c.vaults[0]=ReleaseAssertions.VaultCheck(A,A,1,H,1,true,true,true,true);
        address[] memory destinations=new address[](1);destinations[0]=A;
        ReleaseAssertions.Core memory core=ReleaseAssertions.Core(A,A,A,A,A,A,A,A,1,1,1,1,1,1,true,destinations);
        assertEq(abi.encodeCall(ReleaseAssertions.assertR4,(c,core)),ref("function_ReleaseAssertions_assertR4"));
        assertEq(abi.encodeCall(ReleaseAssertions.assertHandoff,(c,H)),ref("function_ReleaseAssertions_assertHandoff"));
        (ReleaseAssertions.Common memory d,ReleaseAssertions.Core memory k)=abi.decode(abi.encode(c,core),(ReleaseAssertions.Common,ReleaseAssertions.Core));
        assertEq(d.roles[0].account,A);assertEq(d.vaults[0].termsHash,H);assertTrue(d.vaults[0].residualRecovery);assertEq(k.destinations[0],A);
    }
    function test_factoryStringsAndRoundTuple() public view {
        YieldVault.RoundParams memory round=YieldVault.RoundParams(1,1,1,1,1,1,1,1,1);
        YieldVault.InitParams memory p=YieldVault.InitParams(A,A,A,A,A,"Foundation","Foundation",round,A);
        assertEq(abi.encodeCall(YieldVaultFactory.createYieldVault,(p)),ref("function_YieldVaultFactory_createYieldVault"));
        assertEq(abi.encode(string("\x00leading")),ref("abi_string_3"));
        // Solidity retains the NUL; pinned viem decoder trims it. Encoding parity
        // and viem admission compatibility are distinct, explicitly tested claims.
        assertEq(bytes(abi.decode(ref("abi_string_3"),(string))),bytes("\x00leading"));
    }
    function test_OZSingleBatchEncodingAndHash() public {
        address[] memory empty=new address[](0);TimelockController tl=new TimelockController(1,empty,empty,address(this));
        address[] memory targets=new address[](1);targets[0]=A;
        uint256[] memory values=new uint256[](1);values[0]=1;
        bytes[] memory data=new bytes[](1);data[0]=hex"1234";
        assertEq(abi.encodeCall(TimelockController.scheduleBatch,(targets,values,data,H,H,1)),ref("function_TimelockController_scheduleBatch"));
        assertEq(abi.encodeCall(TimelockController.schedule,(A,1,hex"1234",H,H,1)),ref("function_TimelockController_schedule"));
        assertEq(tl.hashOperationBatch(targets,values,data,H,H),keccak256(abi.encode(targets,values,data,H,H)));
        assertEq(tl.hashOperation(A,1,hex"1234",H,H),keccak256(abi.encode(A,uint256(1),hex"1234",H,H)));
        assertNotEq(tl.hashOperationBatch(targets,values,data,H,H),tl.hashOperation(A,1,hex"1234",H,H));
    }
    function test_distinctAssertionFields() public view {
        ReleaseAssertions.Common memory c;
        c.chainId=1672;c.timelock=address(1);c.timelockHash=bytes32(uint256(1));c.minDelay=172800;
        c.gov=address(2);c.can=address(3);c.guardian=address(4);c.ops=address(5);c.deployer=address(6);c.proxyAdmin=address(7);
        c.proxyAdminHash=bytes32(uint256(2));c.inventoryHash=bytes32(uint256(3));
        c.roles=new ReleaseAssertions.RoleCheck[](2);
        c.roles[0]=ReleaseAssertions.RoleCheck(address(0x10),bytes32(uint256(4)),address(0x11),true);
        c.roles[1]=ReleaseAssertions.RoleCheck(address(0x12),bytes32(uint256(5)),address(0x13),false);
        c.proxies=new ReleaseAssertions.ProxyCheck[](1);c.proxies[0]=ReleaseAssertions.ProxyCheck(address(0x20),address(0x21),address(0x22),bytes32(uint256(6)),true,false);
        c.beacons=new ReleaseAssertions.BeaconCheck[](1);c.beacons[0]=ReleaseAssertions.BeaconCheck(address(0x30),address(0x31),address(0x32),bytes32(uint256(7)),false,true);
        c.vaults=new ReleaseAssertions.VaultCheck[](1);c.vaults[0]=ReleaseAssertions.VaultCheck(address(0x40),address(0x41),99,bytes32(uint256(8)),3,true,false,true,false);
        address[] memory destinations=new address[](2);destinations[0]=address(0x60);destinations[1]=address(0x61);
        ReleaseAssertions.Core memory core=ReleaseAssertions.Core(address(0x50),address(0x51),address(0x52),address(0x53),address(0x54),address(0x55),address(0x56),address(0x57),100000,200000,123456789,3600,604800,16,true,destinations);
        bytes memory frozen=vm.parseJsonBytes(vm.readFile("test/fixtures/native-foundation/distinct.json"),".calldata");
        assertEq(abi.encodeCall(ReleaseAssertions.assertR4,(c,core)),frozen);
        bytes memory args=new bytes(frozen.length-4);for(uint256 i;i<args.length;i++)args[i]=frozen[i+4];
        (ReleaseAssertions.Common memory d,ReleaseAssertions.Core memory k)=abi.decode(args,(ReleaseAssertions.Common,ReleaseAssertions.Core));
        assertEq(d.gov,address(2));assertEq(d.can,address(3));assertEq(d.roles[1].account,address(0x13));assertFalse(d.roles[1].expected);
        assertEq(d.vaults[0].round,99);assertEq(d.vaults[0].phase,3);assertFalse(d.vaults[0].residualRecovery);
        assertEq(k.maxWithdrawCount,16);assertEq(k.commissionPpm,100000);assertEq(k.destinations[1],address(0x61));
    }
    function hexRef(string memory name) internal view returns(bytes memory) {
        return vm.parseJsonBytes(vm.readFile("test/fixtures/native-foundation/hex-spelling.json"),string.concat(".",name));
    }
    function test_hexSpellingNestedOctets() public view {
        bytes memory encoded=hexRef("nested-tuple");
        (bytes memory dynamicValue,bytes4 fixedValue,bytes[] memory list,bytes2[2] memory fixedList,HexFields memory nested)=
            abi.decode(encoded,(bytes,bytes4,bytes[],bytes2[2],HexFields));
        assertEq(dynamicValue,hex"abcd00ff");assertEq(fixedValue,bytes4(hex"ababcdef"));
        assertEq(list.length,3);assertEq(list[0],hex"ab");assertEq(list[1],hex"ab");assertEq(list[2],hex"");
        assertEq(fixedList[0],bytes2(hex"abab"));assertEq(fixedList[1],bytes2(hex"abab"));
        assertEq(nested.tag,bytes3(hex"ababff"));assertEq(nested.amount,43981);
        assertEq(nested.target,address(bytes20(hex"abababababababababababababababababababab")));
        assertEq(nested.memo,unicode"ABab 汉字");
        assertEq(nested.payloads.length,3);assertEq(nested.payloads[1].length,0);
        assertEq(nested.payloads[0][0],hex"ab");assertEq(nested.payloads[0][1],hex"ab");
        assertEq(nested.payloads[2][0],hex"");assertEq(nested.payloads[2][1],hex"abcd");
        assertEq(abi.encode(dynamicValue,fixedValue,list,fixedList,nested),encoded);
    }
    function test_hexSpellingOZOperationOctets() public {
        address[] memory empty=new address[](0);TimelockController tl=new TimelockController(1,empty,empty,address(this));
        {
        (address target,uint256 value,bytes memory data,bytes32 predecessor,bytes32 salt)=
            abi.decode(hexRef("timelock-single"),(address,uint256,bytes,bytes32,bytes32));
        assertEq(data,hex"abab00ff");assertEq(value,17);
        assertEq(abi.encodePacked(tl.hashOperation(target,value,data,predecessor,salt)),hexRef("timelock-single-hash"));
        }
        {
        (address[] memory targets,uint256[] memory values,bytes[] memory payloads,bytes32 pred,bytes32 batchSalt)=
            abi.decode(hexRef("timelock-batch"),(address[],uint256[],bytes[],bytes32,bytes32));
        assertEq(payloads.length,2);assertEq(payloads[0],hex"abab00ff");assertEq(payloads[1],hex"abab00ff");
        assertEq(values[1],18);
        assertEq(abi.encode(targets,values,payloads,pred,batchSalt),hexRef("timelock-batch"));
        assertEq(abi.encodePacked(tl.hashOperationBatch(targets,values,payloads,pred,batchSalt)),hexRef("timelock-batch-hash"));
        }
    }
    function decodeBool(bytes memory data) external pure returns(bool){return abi.decode(data,(bool));}
    function decodeUint8(bytes memory data) external pure returns(uint8){return abi.decode(data,(uint8));}
    function test_SolidityMalformedWidthWitnessIsSeparateFromViemAdmission() public {
        vm.expectRevert();this.decodeBool(abi.encode(uint256(2)));
        vm.expectRevert();this.decodeUint8(abi.encode(uint256(256)));
    }
}
